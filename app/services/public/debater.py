"""`GET /public/debaters/{id}`: a debater's whole page in one response.

Their own history (speeches, results, per-tournament rows) is computed live, so it is current
right after an import. Figures that rank them against everyone (percentile, speaker rank) and
the content / style / strategy averages come from the materialized `DebaterProfile`, and
`profile_computed_at` is null until `POST /stats/refresh` has run.
"""

from collections import defaultdict
from datetime import date, datetime
from statistics import pstdev

from sqlmodel import Session, SQLModel, select

from app.models import (
    Ballot,
    Debate,
    Debater,
    DebaterProfile,
    Motion,
    Round,
    Side,
    SpeakerScore,
    Team,
    TeamMember,
)
from app.services.public import field, scores
from app.services.public.loaders import (
    debater_refs,
    institution_refs,
    round_ref,
    team_refs,
)
from app.services.public.schemas import (
    InstitutionRef,
    Metric,
    PersonRef,
    RoundRef,
    SideSplit,
    TeamRef,
    TournamentRef,
    WinLoss,
    metric,
    win_loss,
)
from app.services.public.snapshots import tournament_snapshot
from app.services.stats import core
from app.services.stats.history import tournament_averages


class DebaterSummary(SQLModel):
    tournaments: int
    debates: int
    speeches: int  # prelim speeches counted in the average
    average: Metric
    stdev: Metric
    prelim_win_rate: Metric
    speaker_rank: int | None
    content: Metric
    style: Metric
    strategy: Metric
    iron_person_count: int
    prelim: WinLoss
    elim: WinLoss
    sides: SideSplit


class DebaterTournamentRow(SQLModel):
    tournament: TournamentRef
    team: TeamRef | None
    partners: list[PersonRef]
    average: float | None
    speeches: int
    rank: int | None
    ranked_speakers: int
    team_rank: int | None
    teams: int
    field_average: float | None
    prelim: WinLoss
    elim: WinLoss
    finish: str | None
    bracket: str | None


class PositionCell(SQLModel):
    side: Side
    position: int  # 1-3 substantive, 4 reply (own scale: compare reply to reply only)
    average: float | None
    count: int
    field_mean: float | None
    delta: float | None


class SpeechRow(SQLModel):
    debate_id: int
    tournament: TournamentRef
    round: RoundRef
    side: Side
    position: int
    score: float
    content: float | None
    style: float | None
    strategy: float | None
    team: TeamRef
    opponent: TeamRef
    won: bool | None
    margin: float | None  # panel-averaged team total, own side minus opponent
    motion: str | None


class PartnerRow(SQLModel):
    debater: PersonRef
    tournaments: int


class PublicDebater(SQLModel):
    debater: PersonRef
    institution: InstitutionRef | None
    profile_computed_at: datetime | None
    summary: DebaterSummary
    tournaments: list[DebaterTournamentRow]
    positions: list[PositionCell]
    speeches: list[SpeechRow]
    partners: list[PartnerRow]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_public_debater(session: Session, debater: Debater) -> PublicDebater:
    did = debater.id
    means = field.field_means(session)
    profile = session.exec(select(DebaterProfile).where(DebaterProfile.debater_id == did)).first()

    # Teams, and every debate those teams played.
    team_rows = session.exec(
        select(Team).join(TeamMember, TeamMember.team_id == Team.id).where(TeamMember.debater_id == did)
    ).all()
    my_team_ids = {t.id for t in team_rows}
    debates = (
        session.exec(
            select(Debate, Round)
            .join(Round, Debate.round_id == Round.id)
            .where(Debate.prop_team_id.in_(my_team_ids) | Debate.opp_team_id.in_(my_team_ids))
        ).all()
        if my_team_ids
        else []
    )
    debate_info = {d.id: (d, r) for d, r in debates}
    records = [
        r for r in core.participation(session, debate_ids=list(debate_info)) if r.debater_id == did
    ]
    record_by_debate = {r.debate_id: r for r in records}

    # Counted speeches, one query; grouped per (debate, side, position) into a panel.
    panels: dict[tuple[int, Side, int], list[SpeakerScore]] = defaultdict(list)
    for score, debate_id in session.exec(
        select(SpeakerScore, Ballot.debate_id)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .where(
            SpeakerScore.debater_id == did,
            Ballot.discarded == False,  # noqa: E712
            Ballot.forfeit == False,  # noqa: E712
        )
    ).all():
        panels[(debate_id, score.side, int(score.position))].append(score)
        if debate_id not in debate_info:  # scored without being on the roster (data quirk)
            debate = session.get(Debate, debate_id)
            debate_info[debate_id] = (debate, session.get(Round, debate.round_id))

    all_debate_ids = list(debate_info)
    margins = scores.debate_margins(session, debate_ids=all_debate_ids)
    motions = {
        m.round_id: m.text
        for m in session.exec(
            select(Motion).where(Motion.round_id.in_({r.id for _d, r in debate_info.values()} or {-1}))
        ).all()
    }
    team_ids = {t for d, _r in debate_info.values() for t in (d.prop_team_id, d.opp_team_id)} | my_team_ids
    teams = team_refs(session, team_ids)

    tournament_ids = {r.tournament_id for _d, r in debate_info.values()} | {t.tournament_id for t in team_rows}
    snapshots = {tid: tournament_snapshot(session, tid) for tid in sorted(tournament_ids)}
    snapshots = {tid: snap for tid, snap in snapshots.items() if snap is not None}

    # --- speeches
    speech_rows: list[SpeechRow] = []
    for (debate_id, side, position), panel in panels.items():
        debate, round_ = debate_info[debate_id]
        own_team = debate.prop_team_id if side == Side.PROP else debate.opp_team_id
        opponent = debate.opp_team_id if side == Side.PROP else debate.prop_team_id
        margin = margins.get(debate_id)
        speech_rows.append(
            SpeechRow(
                debate_id=debate_id,
                tournament=snapshots[round_.tournament_id].tournament,
                round=round_ref(round_),
                side=side,
                position=position,
                score=round(_mean([p.final_score for p in panel]), 4),
                content=_rounded(_mean([p.content for p in panel if p.content is not None])),
                style=_rounded(_mean([p.style for p in panel if p.style is not None])),
                strategy=_rounded(_mean([p.strategy for p in panel if p.strategy is not None])),
                team=teams[own_team],
                opponent=teams[opponent],
                won=None if debate.winner is None else debate.winner == side,
                margin=None if margin is None else (margin if side == Side.PROP else -margin),
                motion=motions.get(round_.id),
            )
        )
    speech_rows.sort(
        key=lambda s: (s.tournament.date or date.min, s.tournament.id, s.round.seq, s.position), reverse=True
    )

    # --- per-round normalized prelim scores (the tab's definition: panel mean, replies dropped,
    # max over positions spoken).
    prelim_rows = [
        (did, debate_info[debate_id][1].id, position, _mean([p.final_score for p in panel]))
        for (debate_id, _side, position), panel in panels.items()
        if not debate_info[debate_id][1].isElimination
    ]
    # panel means per (debate, position) are already averaged, so treat each as one entry
    normalized = core.normalize_round_scores(prelim_rows).get(did, {})
    round_tournament = {r.id: r.tournament_id for _d, r in debate_info.values()}
    by_tournament_scores: dict[int, list[float]] = defaultdict(list)
    for round_id, (value, _iron) in normalized.items():
        by_tournament_scores[round_tournament[round_id]].append(value)
    all_prelim = [v for vs in by_tournament_scores.values() for v in vs]
    average = _mean(all_prelim)
    stdev = pstdev(all_prelim) if all_prelim else None

    # --- win / loss
    prelim_wl = [0, 0]
    elim_wl = [0, 0]
    side_wl = {Side.PROP: [0, 0], Side.OPP: [0, 0]}
    per_tournament_wl: dict[int, dict[str, list[int]]] = defaultdict(lambda: {"prelim": [0, 0], "elim": [0, 0]})
    for record in records:
        if record.won is None:
            continue
        index = 0 if record.won else 1
        (elim_wl if record.is_elimination else prelim_wl)[index] += 1
        side_wl[record.side][index] += 1
        per_tournament_wl[record.tournament_id]["elim" if record.is_elimination else "prelim"][index] += 1

    # --- positions (prelim only; reply kept apart because it is on its own scale)
    cells: dict[tuple[Side, int], list[float]] = defaultdict(list)
    for (debate_id, side, position), panel in panels.items():
        if not debate_info[debate_id][1].isElimination:
            cells[(side, position)].append(_mean([p.final_score for p in panel]))
    positions = []
    for side in (Side.PROP, Side.OPP):
        for position in (1, 2, 3, 4):
            values = cells.get((side, position), [])
            avg = _mean(values)
            reference = means.position_means.get((side.value, position))
            positions.append(
                PositionCell(
                    side=side,
                    position=position,
                    average=_rounded(avg),
                    count=len(values),
                    field_mean=reference,
                    delta=None if avg is None or reference is None else round(avg - reference, 2),
                )
            )

    # --- per-tournament rows
    field_average = {t.tournament_id: t.average for t in tournament_averages(session)}
    partner_ids: dict[int, set[int]] = defaultdict(set)
    tournaments_rows: list[DebaterTournamentRow] = []
    for team in team_rows:
        snap = snapshots.get(team.tournament_id)
        if snap is None:
            continue
        info = snap.teams[team.id]
        for other in info.debater_ids:
            if other != did:
                partner_ids[other].add(team.tournament_id)
    people = debater_refs(session, partner_ids)
    for team in team_rows:
        snap = snapshots.get(team.tournament_id)
        if snap is None:
            continue
        info = snap.teams[team.id]
        entry = snap.tab_by_debater.get(did)
        standing = snap.standing_by_team.get(team.id)
        finish = snap.finishes.get(team.id)
        wl = per_tournament_wl.get(team.tournament_id, {"prelim": [0, 0], "elim": [0, 0]})
        tournaments_rows.append(
            DebaterTournamentRow(
                tournament=snap.tournament,
                team=teams[team.id],
                partners=[people[o] for o in info.debater_ids if o != did and o in people],
                average=_rounded(_mean(by_tournament_scores.get(team.tournament_id, []))),
                speeches=len(by_tournament_scores.get(team.tournament_id, [])),
                rank=entry.rank if entry else None,
                ranked_speakers=sum(1 for e in snap.tab if e.rank is not None),
                team_rank=standing.rank if standing else None,
                teams=len(snap.teams),
                field_average=field_average.get(team.tournament_id),
                prelim=win_loss(*wl["prelim"]),
                elim=win_loss(*wl["elim"]),
                finish=finish.finish if finish else None,
                bracket=finish.bracket_label if finish else None,
            )
        )
    tournaments_rows.sort(key=lambda r: (r.tournament.date or date.min, r.tournament.id), reverse=True)

    institution = (
        institution_refs(session, [debater.institution_id]).get(debater.institution_id)
        if debater.institution_id is not None
        else None
    )
    if institution is None and tournaments_rows:
        # Imported debaters often have no school of their own: use the latest team's.
        institution = next((r.team.institution for r in tournaments_rows if r.team and r.team.institution), None)
    prelim_total = sum(prelim_wl)
    summary = DebaterSummary(
        tournaments=len({r.tournament_id for r in records} | {t.tournament_id for t in team_rows}),
        debates=len(records),
        speeches=len(all_prelim),
        average=metric(average, means.debater_average, profile.speaker_score_percentile if profile else None),
        stdev=metric(stdev, means.debater_stdev),
        prelim_win_rate=metric(
            prelim_wl[0] / prelim_total * 100 if prelim_total else None,
            means.debater_prelim_win_rate,
            profile.win_rate_prelims_percentile if profile else None,
            digits=1,
        ),
        speaker_rank=profile.speaker_rank if profile else None,
        content=metric(profile.average_content if profile else None, means.debater_content),
        style=metric(profile.average_style if profile else None, means.debater_style),
        strategy=metric(profile.average_strategy if profile else None, means.debater_strategy),
        iron_person_count=profile.iron_person_count if profile else 0,
        prelim=win_loss(*prelim_wl),
        elim=win_loss(*elim_wl),
        sides=SideSplit(prop=win_loss(*side_wl[Side.PROP]), opp=win_loss(*side_wl[Side.OPP])),
    )
    return PublicDebater(
        debater=PersonRef(id=did, name=_name(debater)),
        institution=institution,
        profile_computed_at=profile.computed_at if profile else means.profile_computed_at,
        summary=summary,
        tournaments=tournaments_rows,
        positions=positions,
        speeches=speech_rows,
        partners=sorted(
            (PartnerRow(debater=people[p], tournaments=len(t)) for p, t in partner_ids.items() if p in people),
            key=lambda r: (-r.tournaments, r.debater.name),
        ),
    )


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _name(debater: Debater) -> str:
    return core.display_name(debater.full_name, debater.first_name, debater.last_name) or f"Debater {debater.id}"

