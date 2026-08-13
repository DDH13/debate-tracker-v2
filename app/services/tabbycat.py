"""Import a tournament from a Tabbycat instance into the local schema.

Tabbycat's model is richer than this app's two-team (prop/opp) schema, so records that
don't fit (BP-style debates, extra motions, unmatched score criteria, etc.) are skipped
rather than aborting the whole import; every skip is recorded on ``ImportReport.skipped``.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from typing import NamedTuple

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError
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

logger = logging.getLogger(__name__)

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


def describe_integrity_error(exc: IntegrityError) -> str:
    """'SQLITE_CONSTRAINT_UNIQUE: UNIQUE constraint failed: teammember.team_id, teammember.debater_id [params: (152, 531)]'"""
    orig = getattr(exc, "orig", None)
    errorname = getattr(orig, "sqlite_errorname", None)
    message = orig.args[0] if orig is not None and orig.args else str(orig or exc)
    parts = [part for part in (errorname, message) if part]
    detail = ": ".join(parts) if parts else str(exc)
    params = getattr(exc, "params", None)
    if params:
        detail = f"{detail} [params: {params}]"
    return detail


def _trunc(obj: object, limit: int = 2000) -> str:
    text = repr(obj)
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


@contextmanager
def _record_context(kind: str, payload: object) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        if not getattr(exc, "_dt_logged", False):
            exc._dt_logged = True  # sentinel: don't double-log through nested frames
            detail = describe_integrity_error(exc) if isinstance(exc, IntegrityError) else type(exc).__name__
            logger.error(
                "failed importing %s: %s\n  upstream payload: %s\n  statement: %s",
                kind,
                detail,
                _trunc(payload),
                getattr(exc, "statement", None),
                exc_info=True,
            )
        raise


def _skip(report: ImportReport, message: str) -> None:
    report.skipped.append(message)
    logger.warning(message)


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
        start = time.perf_counter()
        response = self._client.get(url, headers=self._headers)
        elapsed_ms = (time.perf_counter() - start) * 1000
        if response.status_code in (401, 403):
            logger.warning("upstream GET %s failed: %s %s", url, response.status_code, response.text[:200])
            raise TabbycatImportError(
                f"API key required or invalid for {url}", status_code=response.status_code
            )
        if response.status_code == 404:
            logger.warning("upstream GET %s failed: %s %s", url, response.status_code, response.text[:200])
            raise TabbycatImportError(
                f"tournament {self.slug} not found at {self.base_url}", status_code=404
            )
        if response.status_code >= 400:
            logger.warning("upstream GET %s failed: %s %s", url, response.status_code, response.text[:200])
            raise TabbycatImportError(
                f"Tabbycat request to {url} failed with {response.status_code}: {response.text}",
                status_code=response.status_code,
            )
        logger.debug("GET %s -> %s in %.0fms (%d bytes)", url, response.status_code, elapsed_ms, len(response.content))
        return response.json()

    def get_list(self, url: str) -> list:
        items: list = []
        next_url: str | None = url
        pages = 0
        while next_url:
            data = self.get(next_url)
            pages += 1
            if isinstance(data, list):
                items.extend(data)
                next_url = None
            else:
                items.extend(data.get("results", []))
                next_url = data.get("next")
        logger.debug("GET list %s -> %d items across %d page(s)", url, len(items), pages)
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


class _PersonResolution(NamedTuple):
    person: object
    created: bool
    how: str  # "created" | "cache-email" | "cache-name" | "db-email" | "db-name"
    key: tuple


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
    source_url: str | None = None,
) -> _PersonResolution:
    name = (name or "").strip()
    email = email.strip() if email else None
    key = ("email", email.lower()) if email else ("name", name, institution_id)

    cached = cache.get(key)
    if cached is not None:
        how = "cache-email" if key[0] == "email" else "cache-name"
        logger.debug(
            "%s %r resolved via %s cache key=%r (upstream %s)", model.__name__, name, how, key, source_url
        )
        return _PersonResolution(cached, False, how, key)

    if key[0] == "email":
        existing = session.exec(select(model).where(model.email == email)).first()
    else:
        existing = session.exec(
            select(model).where(model.full_name == name, model.institution_id == institution_id)
        ).first()
    if existing is not None:
        cache[key] = existing
        how = "db-email" if key[0] == "email" else "db-name"
        logger.debug(
            "%s %r resolved via %s to existing id=%s key=%r (upstream %s)",
            model.__name__,
            name,
            how,
            existing.id,
            key,
            source_url,
        )
        return _PersonResolution(existing, False, how, key)

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
    logger.debug("%s %r created id=%s key=%r (upstream %s)", model.__name__, name, person.id, key, source_url)
    return _PersonResolution(person, True, "created", key)


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
    with _record_context(
        f"ballots for debate {debate.id}", {"round_seq": round_seq, "debate_pk": debate_pk}
    ):
        ballots_url = client.url(
            f"/api/v1/tournaments/{client.slug}/rounds/{round_seq}/pairings/{debate_pk}/ballots?confirmed=true"
        )
        submissions = client.get_list(ballots_url)
        logger.debug("debate %s: %d ballot submission(s) fetched", debate.id, len(submissions))

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
                        _skip(
                            report,
                            f"Debate {debate.id}: ballot adjudicator not in imported panel, skipped",
                        )
                        continue
                else:
                    judge_id = chair_judge_id
                    if judge_id is None:
                        _skip(
                            report,
                            f"Debate {debate.id}: consensus ballot has no chair to attribute, skipped",
                        )
                        continue
                if judge_id not in judge_ids_on_panel:
                    _skip(
                        report,
                        f"Debate {debate.id}: ballot judge {judge_id} not on imported panel, skipped",
                    )
                    continue

                existing = sheet_by_judge.get(judge_id)
                if existing is not None:
                    _skip(
                        report,
                        f"Debate {debate.id}: duplicate ballot for judge {judge_id}, kept highest version",
                    )
                    if version <= existing[0]:
                        continue
                sheet_by_judge[judge_id] = (version, sheet, submission)

        any_ballot = False
        debate_ballots = 0
        debate_scores = 0
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
                _skip(
                    report,
                    f"Debate {debate.id}: ballot for judge {judge_id} has no confirmed winner, skipped",
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
            debate_ballots += 1
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
                    _skip(
                        report,
                        f"Debate {debate.id} judge {judge_id}: {len(speeches)} speeches, kept first 4",
                    )
                for index, speech in enumerate(speeches[:4]):
                    debater_id = debater_by_url.get(speech.get("speaker"))
                    if debater_id is None:
                        _skip(
                            report,
                            f"Debate {debate.id} judge {judge_id}: speech speaker not found, skipped",
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
                        _skip(
                            report,
                            f"Debate {debate.id} judge {judge_id}: invalid speaker score, skipped ({exc})",
                        )
                        continue

                    row = SpeakerScore.model_validate(score_in, update={"ballot_id": ballot.id})
                    session.add(row)
                    report.speaker_scores += 1
                    debate_scores += 1
            session.flush()

        if any_ballot:
            _recompute_debate_winner(session, debate)

        logger.info("debate %s: %d ballot(s), %d score(s)", debate.id, debate_ballots, debate_scores)


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
    *,
    report_sink: list[ImportReport] | None = None,
) -> ImportReport:
    logger.info("import start slug=%s base_url=%s include_ballots=%s", slug, base_url, include_ballots)
    existing = session.exec(select(Tournament).where(Tournament.slug == slug)).first()
    if existing is not None:
        raise TournamentAlreadyExists(slug)

    tourney_data = client.get(client.url(f"/api/v1/tournaments/{slug}"))
    with _record_context(f"tournament {slug}", tourney_data):
        tournament = Tournament(
            name=tourney_data["name"],
            abbr=tourney_data.get("short_name") or None,
            base_url=base_url,
            slug=slug,
        )
        session.add(tournament)
        session.flush()
    report = ImportReport(tournament_id=tournament.id)
    if report_sink is not None:
        report_sink.append(report)
    logger.info("tournament local_id=%s name=%r abbr=%r", tournament.id, tournament.name, tournament.abbr)

    institution_by_url: dict[str, int] = {}
    institutions_fetched = 0
    institutions_matched = 0
    for inst in client.get_list(client.url("/api/v1/institutions")):
        institutions_fetched += 1
        with _record_context(f"institution {inst.get('name')!r}", inst):
            existing_inst = session.exec(select(Institution).where(Institution.name == inst["name"])).first()
            if existing_inst is not None:
                institution_by_url[inst["url"]] = existing_inst.id
                institutions_matched += 1
                logger.debug("institution %r matched existing id=%s", inst["name"], existing_inst.id)
                continue
            new_inst = Institution(name=inst["name"], code=inst.get("code") or None)
            session.add(new_inst)
            session.flush()
            institution_by_url[inst["url"]] = new_inst.id
            report.institutions += 1
            logger.debug("institution %r created id=%s code=%r", inst["name"], new_inst.id, inst.get("code"))
    logger.info(
        "institutions: %d fetched, %d created, %d matched by exact name",
        institutions_fetched,
        report.institutions,
        institutions_matched,
    )

    team_by_url: dict[str, int] = {}
    debater_by_url: dict[str, int] = {}
    debater_cache: dict[tuple, object] = {}
    teams_fetched = 0
    teams_no_institution_url = 0
    teams_institution_unresolved = 0
    debaters_reused = 0
    degenerate_person_merges_teams = 0
    for team_data in client.get_list(client.url(f"/api/v1/tournaments/{slug}/teams")):
        teams_fetched += 1
        name = team_data.get("long_name") or team_data.get("short_name") or team_data.get("reference")
        with _record_context(f"team {name!r} (upstream {team_data.get('url')})", team_data):
            institution_url = team_data.get("institution")
            if institution_url is None:
                institution_id = None
                teams_no_institution_url += 1
            else:
                institution_id = institution_by_url.get(institution_url)
                if institution_id is None:
                    teams_institution_unresolved += 1
                    _skip(
                        report,
                        f"Team {name!r}: institution {institution_url} did not resolve, institution_id left NULL",
                    )

            team = Team(tournament_id=tournament.id, name=name, institution_id=institution_id)
            session.add(team)
            session.flush()
            team_by_url[team_data["url"]] = team.id
            report.teams += 1
            logger.debug("team %r created id=%s institution_id=%s", name, team.id, institution_id)

            seen_debaters: dict[int, str] = {}
            for speaker in team_data.get("speakers", []):
                res = _upsert_person(
                    session,
                    Debater,
                    debater_cache,
                    name=speaker.get("name", ""),
                    last_name=speaker.get("last_name"),
                    email=speaker.get("email"),
                    phone=speaker.get("phone"),
                    gender=speaker.get("gender"),
                    institution_id=institution_id,
                    source_url=speaker.get("url"),
                )
                debater = res.person
                if res.created:
                    report.debaters += 1
                else:
                    debaters_reused += 1
                    if res.how == "db-name" and res.key[2] is None:
                        logger.warning(
                            "debater %r (upstream %s) matched pre-existing id=%s by exact name alone "
                            "(no email, no institution) — possible cross-tournament merge",
                            speaker.get("name"),
                            speaker.get("url"),
                            debater.id,
                        )
                        degenerate_person_merges_teams += 1
                debater_by_url[speaker["url"]] = debater.id

                if debater.id in seen_debaters:
                    logger.warning(
                        "team %r: speaker %s (%r) resolved to debater id=%s already on this team via %s "
                        "[resolution=%s key=%r] — duplicate TeamMember skipped (would violate uniq(team_id, debater_id))",
                        name,
                        speaker["url"],
                        speaker.get("name"),
                        debater.id,
                        seen_debaters[debater.id],
                        res.how,
                        res.key,
                    )
                    _skip(
                        report,
                        f"Team {name!r}: speaker {speaker.get('name')!r} resolved to a debater already "
                        f"on this team, duplicate TeamMember skipped",
                    )
                    continue
                seen_debaters[debater.id] = speaker["url"]
                session.add(TeamMember(team_id=team.id, debater_id=debater.id))
            session.flush()

    if teams_no_institution_url:
        _skip(
            report,
            f"{teams_no_institution_url} of {teams_fetched} teams had no institution in the upstream "
            f"payload (institution_id left NULL)",
        )
    if degenerate_person_merges_teams:
        _skip(
            report,
            f"{degenerate_person_merges_teams} debater(s) matched a pre-existing row by name alone "
            f"(no email or institution) — possible cross-tournament identity merge",
        )
    logger.info(
        "teams: %d created, %d debaters created, %d reused, %d teams with unresolved institution",
        report.teams,
        report.debaters,
        debaters_reused,
        teams_institution_unresolved,
    )

    judge_by_url: dict[str, int] = {}
    judge_cache: dict[tuple, object] = {}
    adjs_fetched = 0
    adjs_no_institution = 0
    judges_reused = 0
    degenerate_person_merges_adjs = 0
    for adj in client.get_list(client.url(f"/api/v1/tournaments/{slug}/adjudicators")):
        adjs_fetched += 1
        with _record_context(f"adjudicator {adj.get('name')!r}", adj):
            institution_url = adj.get("institution")
            if institution_url is None:
                institution_id = None
                adjs_no_institution += 1
            else:
                institution_id = institution_by_url.get(institution_url)
                if institution_id is None:
                    _skip(
                        report,
                        f"Adjudicator {adj.get('name')!r}: institution {institution_url} did not resolve, "
                        f"institution_id left NULL",
                    )
            res = _upsert_person(
                session,
                Judge,
                judge_cache,
                name=adj.get("name", ""),
                last_name=adj.get("last_name"),
                email=adj.get("email"),
                phone=adj.get("phone"),
                gender=adj.get("gender"),
                institution_id=institution_id,
                source_url=adj.get("url"),
            )
            judge = res.person
            if res.created:
                report.judges += 1
            else:
                judges_reused += 1
                if res.how == "db-name" and res.key[2] is None:
                    logger.warning(
                        "judge %r (upstream %s) matched pre-existing id=%s by exact name alone "
                        "(no email, no institution) — possible cross-tournament merge",
                        adj.get("name"),
                        adj.get("url"),
                        judge.id,
                    )
                    degenerate_person_merges_adjs += 1
            judge_by_url[adj["url"]] = judge.id
            logger.debug(
                "adjudicator %r resolved to judge id=%s institution_id=%s", adj.get("name"), judge.id, institution_id
            )

    if adjs_no_institution:
        _skip(
            report,
            f"{adjs_no_institution} of {adjs_fetched} adjudicators had no institution in the upstream "
            f"payload (institution_id left NULL)",
        )
    if degenerate_person_merges_adjs:
        _skip(
            report,
            f"{degenerate_person_merges_adjs} judge(s) matched a pre-existing row by name alone "
            f"(no email or institution) — possible cross-tournament identity merge",
        )
    logger.info(
        "adjudicators: %d created, %d reused, %d with no institution",
        report.judges,
        judges_reused,
        adjs_no_institution,
    )

    venue_name_by_url: dict[str, str] = {
        venue["url"]: venue.get("name") or venue.get("display_name")
        for venue in client.get_list(client.url(f"/api/v1/tournaments/{slug}/venues"))
    }

    criterion_name_by_url: dict[str, str] = {
        criterion["url"]: criterion.get("name", "")
        for criterion in client.get_list(client.url(f"/api/v1/tournaments/{slug}/score-criteria"))
    }
    logger.debug(
        "venues: %d, score-criteria: %d (%s)",
        len(venue_name_by_url),
        len(criterion_name_by_url),
        ", ".join(criterion_name_by_url.values()),
    )

    starts_ats: list[date] = []
    round_ids_by_seq: dict[int, int] = {}
    rounds_fetched = 0
    rounds_no_motion = 0
    for round_data in client.get_list(client.url(f"/api/v1/tournaments/{slug}/rounds")):
        rounds_fetched += 1
        with _record_context(f"round seq={round_data.get('seq')}", round_data):
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
            logger.debug("round seq=%s created id=%s", round_data["seq"], round_.id)

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
                    _skip(
                        report,
                        f"Round {round_data['seq']}: kept first of {len(motions)} motions, "
                        f"{len(motions) - 1} extra motion(s) skipped",
                    )
            else:
                rounds_no_motion += 1

    if rounds_no_motion:
        _skip(
            report,
            f"{rounds_no_motion} of {rounds_fetched} rounds had no motion in the upstream payload "
            f"(0 motions imported for them)",
        )
        if rounds_no_motion == rounds_fetched:
            logger.warning("no motions at all in %d round(s) — total motion data loss", rounds_no_motion)
    logger.info(
        "rounds: %d created, %d motions imported, %d rounds with no motion in payload",
        report.rounds,
        report.motions,
        rounds_no_motion,
    )

    for seq, round_id in round_ids_by_seq.items():
        pairings = client.get_list(client.url(f"/api/v1/tournaments/{slug}/rounds/{seq}/pairings"))
        logger.info("round %d: %d pairings fetched", seq, len(pairings))
        round_debates_created = 0
        round_pairings_skipped = 0
        for pairing in pairings:
            with _record_context(f"round {seq} pairing {pairing.get('id')}", pairing):
                teams = pairing.get("teams") or []
                if len(teams) != 2:
                    _skip(
                        report,
                        f"Round {seq} pairing {pairing.get('id')}: expected 2 teams, got {len(teams)}, skipped",
                    )
                    round_pairings_skipped += 1
                    continue

                sides: dict[str, str] = {}
                for team_entry in teams:
                    side = _normalize_side(team_entry.get("side"))
                    if side is None or side in sides:
                        sides = {}
                        break
                    sides[side] = team_entry.get("team")
                if "prop" not in sides or "opp" not in sides:
                    _skip(
                        report,
                        f"Round {seq} pairing {pairing.get('id')}: unmappable sides (BP or bye), skipped",
                    )
                    round_pairings_skipped += 1
                    continue

                prop_team_id = team_by_url.get(sides["prop"])
                opp_team_id = team_by_url.get(sides["opp"])
                if prop_team_id is None or opp_team_id is None:
                    _skip(
                        report,
                        f"Round {seq} pairing {pairing.get('id')}: team not found in imported set, skipped",
                    )
                    round_pairings_skipped += 1
                    continue

                venue_url = pairing.get("venue")
                room = venue_name_by_url.get(venue_url) if venue_url else None

                debate = Debate(
                    round_id=round_id, prop_team_id=prop_team_id, opp_team_id=opp_team_id, room=room
                )
                session.add(debate)
                session.flush()
                report.debates += 1
                round_debates_created += 1
                logger.debug(
                    "round %d debate id=%s: prop=%s opp=%s room=%r", seq, debate.id, prop_team_id, opp_team_id, room
                )

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
                            if judge_id in judge_ids_on_panel:
                                logger.warning(
                                    "debate %s: judge id=%s listed as panellist but already on panel "
                                    "(e.g. chair) — would violate uniq(debate_id, judge_id), skipping duplicate",
                                    debate.id,
                                    judge_id,
                                )
                                continue
                            session.add(DebateJudge(debate_id=debate.id, judge_id=judge_id))
                            judge_ids_on_panel.add(judge_id)
                    for trainee_url in adjudicators.get("trainees") or []:
                        judge_id = judge_by_url.get(trainee_url)
                        if judge_id is not None:
                            if judge_id in judge_ids_on_panel:
                                logger.warning(
                                    "debate %s: judge id=%s listed as trainee but already on panel "
                                    "— would violate uniq(debate_id, judge_id), skipping duplicate",
                                    debate.id,
                                    judge_id,
                                )
                                continue
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
        logger.info(
            "round %d: %d debate(s) created, %d pairing(s) skipped", seq, round_debates_created, round_pairings_skipped
        )

    if starts_ats:
        tournament.date = min(starts_ats)
        session.add(tournament)
        logger.info("tournament.date set to %s from %d round start time(s)", tournament.date, len(starts_ats))
    else:
        _skip(report, "no round had starts_at; tournament.date left NULL")

    logger.info("import complete slug=%s %s", slug, report.model_dump(exclude={"skipped"}))
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
    sink: list[ImportReport] = []
    try:
        report = _run_import(active_session, client, base_url, slug, include_ballots, report_sink=sink)
        active_session.commit()
        return report
    except Exception as exc:
        partial = sink[0] if sink else None
        logger.error("import FAILED slug=%s: %s", slug, exc)
        if partial is not None:
            logger.error("  partial progress before rollback: %s", partial.model_dump(exclude={"skipped"}))
            for entry in partial.skipped:
                logger.warning("  skipped (discarded by rollback): %s", entry)
        active_session.rollback()
        raise
    finally:
        client.close()
        if owns_session:
            active_session.close()
