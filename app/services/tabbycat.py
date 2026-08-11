"""Import a tournament from a Tabbycat instance into the local schema.

Tabbycat's model is richer than this app's two-team (prop/opp) schema, so records that
don't fit (BP-style debates, extra motions, unmatched score criteria, etc.) are skipped
rather than aborting the whole import; every skip is recorded on ``ImportReport.skipped``.
"""

from datetime import date, datetime

import httpx
from pydantic import BaseModel, ValidationError
from sqlmodel import Session, select

from app.core.config import settings
from app.db.session import engine
from app.models import (
    Ballot,
    Debate,
    DebateJudge,
    Debater,
    Institution,
    Judge,
    Motion,
    Round,
    Side,
    SpeakerPosition,
    SpeakerScore,
    SpeakerScoreCreate,
    Team,
    TeamMember,
    Tournament,
)

_GENDER_MAP = {"M": "male", "F": "female", "O": "other"}

_CRITERION_ALIASES = {
    "content": {"content", "matter"},
    "style": {"style", "manner"},
    "strategy": {"strategy", "method"},
}


class TabbycatImportError(Exception):
    """An upstream Tabbycat request failed (non-2xx response)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TournamentAlreadyExists(Exception):
    """A local tournament with the requested slug already exists."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"Tournament with slug '{slug}' already exists")
        self.slug = slug


class ImportReport(BaseModel):
    tournament_id: int
    institutions: int = 0
    teams: int = 0
    debaters: int = 0
    judges: int = 0
    rounds: int = 0
    motions: int = 0
    debates: int = 0
    ballots: int = 0
    speaker_scores: int = 0
    skipped: list[str] = []


class _TabbycatClient:
    """Thin wrapper over httpx.Client that normalises Tabbycat's response shapes."""

    def __init__(
        self,
        base_url: str,
        slug: str,
        api_key: str | None = None,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.slug = slug
        self._headers = {"Authorization": f"Token {api_key}"} if api_key else {}
        self._client = http_client or httpx.Client(timeout=30.0)
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def get(self, url: str) -> dict:
        response = self._client.get(url, headers=self._headers)
        if response.status_code in (401, 403):
            raise TabbycatImportError("API key required or invalid", status_code=response.status_code)
        if response.status_code == 404:
            raise TabbycatImportError(
                f"tournament {self.slug} not found at {self.base_url}", status_code=404
            )
        if response.status_code >= 400:
            raise TabbycatImportError(
                f"Tabbycat request to {url} failed with {response.status_code}: {response.text}",
                status_code=response.status_code,
            )
        return response.json()

    def get_list(self, url: str) -> list:
        items: list = []
        next_url: str | None = url
        while next_url:
            data = self.get(next_url)
            if isinstance(data, list):
                items.extend(data)
                next_url = None
            else:
                items.extend(data.get("results", []))
                next_url = data.get("next")
        return items


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _split_name(name: str, last_name: str | None) -> tuple[str | None, str | None]:
    name = name.strip()
    if last_name:
        if name.endswith(last_name):
            first_name = name[: -len(last_name)].strip() or None
        else:
            first_name = name or None
        return first_name, last_name
    parts = name.split(None, 1)
    return (parts[0] if parts else None), None


def _upsert_person(
    session: Session,
    model: type[Debater] | type[Judge],
    cache: dict[tuple, object],
    *,
    name: str,
    last_name: str | None,
    email: str | None,
    phone: str | None,
    gender: str | None,
    institution_id: int | None,
) -> tuple[object, bool]:
    name = (name or "").strip()
    email = email.strip() if email else None
    key = ("email", email.lower()) if email else ("name", name, institution_id)

    cached = cache.get(key)
    if cached is not None:
        return cached, False

    if key[0] == "email":
        existing = session.exec(select(model).where(model.email == email)).first()
    else:
        existing = session.exec(
            select(model).where(model.full_name == name, model.institution_id == institution_id)
        ).first()
    if existing is not None:
        cache[key] = existing
        return existing, False

    first_name, resolved_last_name = _split_name(name, last_name)
    person = model(
        full_name=name,
        first_name=first_name,
        last_name=resolved_last_name,
        email=email,
        phone=phone or None,
        gender=_GENDER_MAP.get(gender) if gender else None,
        institution_id=institution_id,
    )
    session.add(person)
    session.flush()
    cache[key] = person
    return person, True


def _normalize_side(value) -> str | None:
    if value == "aff" or value == 0:
        return "prop"
    if value == "neg" or value == 1:
        return "opp"
    return None


def _match_criteria(
    criteria: list[dict], criterion_name_by_url: dict[str, str]
) -> tuple[float, float, float] | None:
    scores: dict[str, float] = {}
    for entry in criteria:
        name = criterion_name_by_url.get(entry.get("criterion"))
        if not name:
            continue
        lowered = name.strip().lower()
        for category, aliases in _CRITERION_ALIASES.items():
            if category not in scores and lowered in aliases:
                scores[category] = entry.get("score")
    if all(category in scores for category in _CRITERION_ALIASES):
        return scores["content"], scores["style"], scores["strategy"]
    return None


def _import_ballots(
    session: Session,
    client: _TabbycatClient,
    round_seq: int,
    debate_pk: int,
    debate: Debate,
    *,
    judge_by_url: dict[str, int],
    team_by_url: dict[str, int],
    debater_by_url: dict[str, int],
    criterion_name_by_url: dict[str, str],
    chair_judge_id: int | None,
    judge_ids_on_panel: set[int],
    report: ImportReport,
) -> None:
    ballots_url = client.url(
        f"/api/v1/tournaments/{client.slug}/rounds/{round_seq}/pairings/{debate_pk}/ballots?confirmed=true"
    )
    submissions = client.get_list(ballots_url)

    sheet_by_judge: dict[int, tuple[int, dict, dict]] = {}
    for submission in submissions:
        if submission.get("discarded"):
            continue
        version = submission.get("version") or 0
        for sheet in (submission.get("result") or {}).get("sheets", []):
            adjudicator_url = sheet.get("adjudicator")
            if adjudicator_url:
                judge_id = judge_by_url.get(adjudicator_url)
                if judge_id is None:
                    report.skipped.append(
                        f"Debate {debate.id}: ballot adjudicator not in imported panel, skipped"
                    )
                    continue
            else:
                judge_id = chair_judge_id
                if judge_id is None:
                    report.skipped.append(
                        f"Debate {debate.id}: consensus ballot has no chair to attribute, skipped"
                    )
                    continue
            if judge_id not in judge_ids_on_panel:
                report.skipped.append(
                    f"Debate {debate.id}: ballot judge {judge_id} not on imported panel, skipped"
                )
                continue

            existing = sheet_by_judge.get(judge_id)
            if existing is not None:
                report.skipped.append(
                    f"Debate {debate.id}: duplicate ballot for judge {judge_id}, kept highest version"
                )
                if version <= existing[0]:
                    continue
            sheet_by_judge[judge_id] = (version, sheet, submission)

    any_ballot = False
    for judge_id, (_version, sheet, submission) in sheet_by_judge.items():
        teams = sheet.get("teams") or []
        winner_side = None
        for team_result in teams:
            if team_result.get("win"):
                team_id = team_by_url.get(team_result.get("team"))
                if team_id == debate.prop_team_id:
                    winner_side = Side.PROP
                elif team_id == debate.opp_team_id:
                    winner_side = Side.OPP
        if winner_side is None:
            report.skipped.append(
                f"Debate {debate.id}: ballot for judge {judge_id} has no confirmed winner, skipped"
            )
            continue

        ballot = Ballot(
            debate_id=debate.id,
            judge_id=judge_id,
            winner=winner_side,
            forfeit=bool(submission.get("forfeit", False)),
        )
        session.add(ballot)
        session.flush()
        report.ballots += 1
        any_ballot = True

        for team_result in teams:
            team_id = team_by_url.get(team_result.get("team"))
            if team_id == debate.prop_team_id:
                side = Side.PROP
            elif team_id == debate.opp_team_id:
                side = Side.OPP
            else:
                continue

            speeches = team_result.get("speeches") or []
            if len(speeches) > 4:
                report.skipped.append(
                    f"Debate {debate.id} judge {judge_id}: {len(speeches)} speeches, kept first 4"
                )
            for index, speech in enumerate(speeches[:4]):
                debater_id = debater_by_url.get(speech.get("speaker"))
                if debater_id is None:
                    report.skipped.append(
                        f"Debate {debate.id} judge {judge_id}: speech speaker not found, skipped"
                    )
                    continue

                breakdown = _match_criteria(speech.get("criteria") or [], criterion_name_by_url)
                content, style, strategy = breakdown if breakdown else (None, None, None)
                try:
                    score_in = SpeakerScoreCreate(
                        debater_id=debater_id,
                        side=side,
                        position=SpeakerPosition(index + 1),
                        content=content,
                        style=style,
                        strategy=strategy,
                        final_score=speech.get("score") if breakdown is None else None,
                    )
                except ValidationError as exc:
                    report.skipped.append(
                        f"Debate {debate.id} judge {judge_id}: invalid speaker score, skipped ({exc})"
                    )
                    continue

                row = SpeakerScore.model_validate(score_in, update={"ballot_id": ballot.id})
                session.add(row)
                report.speaker_scores += 1
        session.flush()

    if any_ballot:
        _recompute_debate_winner(session, debate)


def _recompute_debate_winner(session: Session, debate: Debate) -> None:
    """Majority-vote winner. Mirrors `_recompute_debate_winner` in
    app/api/v1/endpoints/ballots.py (kept separate since services shouldn't import
    from the API layer)."""
    ballots = session.exec(select(Ballot).where(Ballot.debate_id == debate.id)).all()
    counted = [b for b in ballots if not b.discarded]
    prop_votes = sum(1 for b in counted if b.winner == Side.PROP)
    opp_votes = sum(1 for b in counted if b.winner == Side.OPP)
    if prop_votes > opp_votes:
        debate.winner = Side.PROP
        session.add(debate)
    elif opp_votes > prop_votes:
        debate.winner = Side.OPP
        session.add(debate)


def _run_import(
    session: Session,
    client: _TabbycatClient,
    base_url: str,
    slug: str,
    include_ballots: bool,
) -> ImportReport:
    existing = session.exec(select(Tournament).where(Tournament.slug == slug)).first()
    if existing is not None:
        raise TournamentAlreadyExists(slug)

    tourney_data = client.get(client.url(f"/api/v1/tournaments/{slug}"))
    tournament = Tournament(
        name=tourney_data["name"],
        abbr=tourney_data.get("short_name") or None,
        base_url=base_url,
        slug=slug,
    )
    session.add(tournament)
    session.flush()
    report = ImportReport(tournament_id=tournament.id)

    institution_by_url: dict[str, int] = {}
    for inst in client.get_list(client.url("/api/v1/institutions")):
        existing_inst = session.exec(select(Institution).where(Institution.name == inst["name"])).first()
        if existing_inst is not None:
            institution_by_url[inst["url"]] = existing_inst.id
            continue
        new_inst = Institution(name=inst["name"], code=inst.get("code") or None)
        session.add(new_inst)
        session.flush()
        institution_by_url[inst["url"]] = new_inst.id
        report.institutions += 1

    team_by_url: dict[str, int] = {}
    debater_by_url: dict[str, int] = {}
    debater_cache: dict[tuple, object] = {}
    for team_data in client.get_list(client.url(f"/api/v1/tournaments/{slug}/teams")):
        name = team_data.get("long_name") or team_data.get("short_name") or team_data.get("reference")
        institution_url = team_data.get("institution")
        institution_id = institution_by_url.get(institution_url) if institution_url else None
        team = Team(tournament_id=tournament.id, name=name, institution_id=institution_id)
        session.add(team)
        session.flush()
        team_by_url[team_data["url"]] = team.id
        report.teams += 1

        for speaker in team_data.get("speakers", []):
            debater, created = _upsert_person(
                session,
                Debater,
                debater_cache,
                name=speaker.get("name", ""),
                last_name=speaker.get("last_name"),
                email=speaker.get("email"),
                phone=speaker.get("phone"),
                gender=speaker.get("gender"),
                institution_id=institution_id,
            )
            if created:
                report.debaters += 1
            debater_by_url[speaker["url"]] = debater.id
            session.add(TeamMember(team_id=team.id, debater_id=debater.id))
        session.flush()

    judge_by_url: dict[str, int] = {}
    judge_cache: dict[tuple, object] = {}
    for adj in client.get_list(client.url(f"/api/v1/tournaments/{slug}/adjudicators")):
        institution_url = adj.get("institution")
        institution_id = institution_by_url.get(institution_url) if institution_url else None
        judge, created = _upsert_person(
            session,
            Judge,
            judge_cache,
            name=adj.get("name", ""),
            last_name=adj.get("last_name"),
            email=adj.get("email"),
            phone=adj.get("phone"),
            gender=adj.get("gender"),
            institution_id=institution_id,
        )
        if created:
            report.judges += 1
        judge_by_url[adj["url"]] = judge.id

    venue_name_by_url: dict[str, str] = {
        venue["url"]: venue.get("name") or venue.get("display_name")
        for venue in client.get_list(client.url(f"/api/v1/tournaments/{slug}/venues"))
    }

    criterion_name_by_url: dict[str, str] = {
        criterion["url"]: criterion.get("name", "")
        for criterion in client.get_list(client.url(f"/api/v1/tournaments/{slug}/score-criteria"))
    }

    starts_ats: list[date] = []
    round_ids_by_seq: dict[int, int] = {}
    for round_data in client.get_list(client.url(f"/api/v1/tournaments/{slug}/rounds")):
        round_ = Round(
            tournament_id=tournament.id,
            seq=round_data["seq"],
            abbr=round_data.get("abbreviation") or None,
            name=round_data.get("name") or None,
            isElimination=round_data.get("stage") == "E",
            completed=bool(round_data.get("completed", False)),
        )
        session.add(round_)
        session.flush()
        report.rounds += 1
        round_ids_by_seq[round_data["seq"]] = round_.id

        starts_at = _parse_datetime(round_data.get("starts_at"))
        if starts_at is not None:
            starts_ats.append(starts_at.date())

        motions = round_data.get("motions") or []
        if motions:
            first_motion = motions[0]
            session.add(
                Motion(
                    round_id=round_.id,
                    text=first_motion["text"],
                    info_slide=first_motion.get("info_slide") or None,
                )
            )
            session.flush()
            report.motions += 1
            if len(motions) > 1:
                report.skipped.append(
                    f"Round {round_data['seq']}: kept first of {len(motions)} motions, "
                    f"{len(motions) - 1} extra motion(s) skipped"
                )

    for seq, round_id in round_ids_by_seq.items():
        pairings = client.get_list(
            client.url(f"/api/v1/tournaments/{slug}/rounds/{seq}/pairings")
        )
        for pairing in pairings:
            teams = pairing.get("teams") or []
            if len(teams) != 2:
                report.skipped.append(
                    f"Round {seq} pairing {pairing.get('id')}: expected 2 teams, got {len(teams)}, skipped"
                )
                continue

            sides: dict[str, str] = {}
            for team_entry in teams:
                side = _normalize_side(team_entry.get("side"))
                if side is None or side in sides:
                    sides = {}
                    break
                sides[side] = team_entry.get("team")
            if "prop" not in sides or "opp" not in sides:
                report.skipped.append(
                    f"Round {seq} pairing {pairing.get('id')}: unmappable sides (BP or bye), skipped"
                )
                continue

            prop_team_id = team_by_url.get(sides["prop"])
            opp_team_id = team_by_url.get(sides["opp"])
            if prop_team_id is None or opp_team_id is None:
                report.skipped.append(
                    f"Round {seq} pairing {pairing.get('id')}: team not found in imported set, skipped"
                )
                continue

            venue_url = pairing.get("venue")
            room = venue_name_by_url.get(venue_url) if venue_url else None

            debate = Debate(round_id=round_id, prop_team_id=prop_team_id, opp_team_id=opp_team_id, room=room)
            session.add(debate)
            session.flush()
            report.debates += 1

            judge_ids_on_panel: set[int] = set()
            chair_judge_id: int | None = None
            adjudicators = pairing.get("adjudicators")
            if adjudicators:
                chair_url = adjudicators.get("chair")
                if chair_url:
                    chair_judge_id = judge_by_url.get(chair_url)
                    if chair_judge_id is not None:
                        session.add(DebateJudge(debate_id=debate.id, judge_id=chair_judge_id, is_chair=True))
                        judge_ids_on_panel.add(chair_judge_id)
                for panellist_url in adjudicators.get("panellists") or []:
                    judge_id = judge_by_url.get(panellist_url)
                    if judge_id is not None:
                        session.add(DebateJudge(debate_id=debate.id, judge_id=judge_id))
                        judge_ids_on_panel.add(judge_id)
                for trainee_url in adjudicators.get("trainees") or []:
                    judge_id = judge_by_url.get(trainee_url)
                    if judge_id is not None:
                        session.add(DebateJudge(debate_id=debate.id, judge_id=judge_id, is_trainee=True))
                        judge_ids_on_panel.add(judge_id)
                session.flush()

            if include_ballots:
                _import_ballots(
                    session,
                    client,
                    seq,
                    pairing["id"],
                    debate,
                    judge_by_url=judge_by_url,
                    team_by_url=team_by_url,
                    debater_by_url=debater_by_url,
                    criterion_name_by_url=criterion_name_by_url,
                    chair_judge_id=chair_judge_id,
                    judge_ids_on_panel=judge_ids_on_panel,
                    report=report,
                )

    if starts_ats:
        tournament.date = min(starts_ats)
        session.add(tournament)

    return report


def import_tournament(
    base_url: str,
    slug: str,
    api_key: str | None = None,
    *,
    session: Session | None = None,
    include_ballots: bool = True,
) -> ImportReport:
    """Fetch a tournament from a Tabbycat instance and populate the local DB.

    Raises `TournamentAlreadyExists` if a local tournament with `slug` already exists,
    and `TabbycatImportError` on upstream HTTP failures. `httpx` transport-level errors
    (timeouts, connection failures) propagate unchanged. With `include_ballots=True`
    this makes `1 + rounds + debates` upstream requests and can take a while for a
    large tournament.
    """
    api_key = api_key or settings.tabbycat_api_key
    owns_session = session is None
    active_session = session or Session(engine)
    client = _TabbycatClient(base_url, slug, api_key)
    try:
        report = _run_import(active_session, client, base_url, slug, include_ballots)
        active_session.commit()
        return report
    except Exception:
        active_session.rollback()
        raise
    finally:
        client.close()
        if owns_session:
            active_session.close()
