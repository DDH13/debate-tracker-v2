"""British Parliamentary statistics: the BP analogue of `app.services.stats.tournament`.

BP has no win/loss and no content/style/strategy breakdown — a debate's result is a
ranking of four teams worth 3/2/1/0 team points. `core.competition_ranks`,
`core.percentile_ranks`, `core.mean_and_stdev` and `core.display_name` are side-agnostic
and reused directly; `core.round_scores` and `core.participation` are two-team-specific
(they key off `Side`/`Ballot.winner`), so this module provides BP counterparts.
"""

import math
from collections import defaultdict
from dataclasses import dataclass

from sqlmodel import Session, SQLModel, func, select

from app.models import (
    BPBallot,
    BPDebate,
    BPDebateTeam,
    BPSide,
    BPSpeakerScore,
    Institution,
    Motion,
    Round,
    Team,
    TeamMember,
)
from app.services.stats import core
from app.services.stats import tournament as tournament_stats


def bp_points_for_rank(rank: int) -> int:
    """3/2/1/0 team points for a 1st/2nd/3rd/4th place finish."""
    return 4 - rank


@dataclass
class BPParticipation:
    """One debater's participation in one BP debate."""

    debater_id: int
    tournament_id: int
    round_id: int
    round_seq: int
    is_elimination: bool
    bp_debate_id: int
    team_id: int
    side: BPSide
    rank: int | None
    points: int | None


class BPSpeakerTabRow(SQLModel):
    rank: int
    debater_id: int
    name: str
    team_id: int | None
    team_name: str | None
    average: float
    speeches: int
    stdev: float


class BPSpeakerTab(SQLModel):
    minimum_speeches: int
    rows: list[BPSpeakerTabRow]


class BPTeamStanding(SQLModel):
    rank: int
    team_id: int
    name: str
    institution: str | None
    prelim_points: int
    elim_points: int
    # Elimination rounds sometimes carry only advance/eliminate (no points), so these
    # count outrounds won/lost independently of `elim_points`.
    elim_advances: int
    elim_eliminations: int
    firsts: int
    seconds: int
    thirds: int
    fourths: int
    total_speaks: float
    average_speaks: float


class BPTournamentSummary(SQLModel):
    tournament_id: int
    institutions: int
    teams: int
    debaters: int
    judges: int
    rounds: int
    prelim_rounds: int
    elim_rounds: int
    debates: int
    ballots: int
    speaker_scores: int
    mean_speaker_score: float | None
    speaker_score_stdev: float | None


class BPSidePointStat(SQLModel):
    side: BPSide
    average_points: float | None
    firsts: int
    seconds: int
    thirds: int
    fourths: int


class BPRoundSideStats(SQLModel):
    round_id: int
    round_seq: int
    round_name: str | None
    sides: list[BPSidePointStat]


class BPSideStats(SQLModel):
    overall: list[BPSidePointStat]
    by_round: list[BPRoundSideStats]


class BPMotionSideStat(SQLModel):
    side: BPSide
    average_points: float | None
    average_speaks: float | None


class BPMotionStat(SQLModel):
    motion_id: int
    round_id: int
    round_seq: int
    text: str
    sides: list[BPMotionSideStat]


def _bp_round_position_averages(
    session: Session, tournament_id: int, *, elimination: bool
) -> dict[int, dict[int, dict[int, float]]]:
    """{debater_id: {round_id: {position: panel-averaged score}}}. BP has no reply speech,
    so unlike the two-team version there is nothing to drop."""
    statement = (
        select(BPSpeakerScore, BPDebate.round_id)
        .join(BPBallot, BPSpeakerScore.bp_ballot_id == BPBallot.id)
        .join(BPDebate, BPBallot.bp_debate_id == BPDebate.id)
        .join(Round, BPDebate.round_id == Round.id)
        .where(
            Round.tournament_id == tournament_id,
            Round.isElimination == elimination,
            BPBallot.discarded == False,  # noqa: E712
            BPBallot.forfeit == False,  # noqa: E712
        )
    )
    rows = session.exec(statement).all()

    panel_buckets: dict[tuple[int, int, int], list[float]] = defaultdict(list)
    for score, round_id in rows:
        panel_buckets[(score.debater_id, round_id, score.position)].append(score.final_score)

    result: dict[int, dict[int, dict[int, float]]] = defaultdict(lambda: defaultdict(dict))
    for (debater_id, round_id, position), values in panel_buckets.items():
        result[debater_id][round_id][position] = sum(values) / len(values)
    return {d: dict(rounds) for d, rounds in result.items()}


def bp_round_scores_with_iron(
    session: Session, tournament_id: int, *, elimination: bool = False
) -> dict[int, dict[int, tuple[float, bool]]]:
    """Like `bp_round_scores` but also flags whether the round score came from an iron
    speech (the debater spoke both positions on their side that round)."""
    position_averages = _bp_round_position_averages(session, tournament_id, elimination=elimination)
    result: dict[int, dict[int, tuple[float, bool]]] = {}
    for debater_id, rounds in position_averages.items():
        result[debater_id] = {
            round_id: (max(positions.values()), len(positions) > 1)
            for round_id, positions in rounds.items()
        }
    return result


def bp_round_scores(
    session: Session, tournament_id: int, *, elimination: bool = False
) -> dict[int, dict[int, float]]:
    """{debater_id: {round_id: score}}, panel-merged (mean across judges) and iron-merged
    (max over positions spoken)."""
    detailed = bp_round_scores_with_iron(session, tournament_id, elimination=elimination)
    return {
        debater_id: {round_id: score for round_id, (score, _is_iron) in rounds.items()}
        for debater_id, rounds in detailed.items()
    }


def bp_participation(session: Session, tournament_id: int | None = None) -> list[BPParticipation]:
    """Per-debate side/rank/points records. A debater participated in a debate if they
    have a BPSpeakerScore row for it (from a non-discarded, non-forfeit ballot); debates
    with no recorded speaker scores at all fall back to full TeamMember roster membership."""
    debate_query = select(BPDebate, Round).join(Round, BPDebate.round_id == Round.id)
    if tournament_id is not None:
        debate_query = debate_query.where(Round.tournament_id == tournament_id)
    debates = session.exec(debate_query).all()
    if not debates:
        return []

    bp_debate_ids = [d.id for d, _ in debates]
    team_rows = session.exec(
        select(BPDebateTeam).where(BPDebateTeam.bp_debate_id.in_(bp_debate_ids))
    ).all()
    teams_by_debate: dict[int, list[BPDebateTeam]] = defaultdict(list)
    for row in team_rows:
        teams_by_debate[row.bp_debate_id].append(row)

    team_ids = {row.team_id for row in team_rows}
    members_by_team: dict[int, set[int]] = defaultdict(set)
    for tm in session.exec(select(TeamMember).where(TeamMember.team_id.in_(team_ids))).all():
        members_by_team[tm.team_id].add(tm.debater_id)

    scored_by_debate: dict[int, set[int]] = defaultdict(set)
    score_rows = session.exec(
        select(BPSpeakerScore.debater_id, BPBallot.bp_debate_id)
        .join(BPBallot, BPSpeakerScore.bp_ballot_id == BPBallot.id)
        .where(
            BPBallot.bp_debate_id.in_(bp_debate_ids),
            BPBallot.discarded == False,  # noqa: E712
            BPBallot.forfeit == False,  # noqa: E712
        )
    ).all()
    for debater_id, bp_debate_id in score_rows:
        scored_by_debate[bp_debate_id].add(debater_id)

    records: list[BPParticipation] = []
    for debate, round_ in debates:
        scored = scored_by_debate.get(debate.id, set())
        for dt in teams_by_debate.get(debate.id, []):
            team_members = members_by_team.get(dt.team_id, set())
            debater_ids = (scored & team_members) if scored else team_members
            for debater_id in debater_ids:
                records.append(
                    BPParticipation(
                        debater_id=debater_id,
                        tournament_id=round_.tournament_id,
                        round_id=round_.id,
                        round_seq=round_.seq,
                        is_elimination=round_.isElimination,
                        bp_debate_id=debate.id,
                        team_id=dt.team_id,
                        side=dt.side,
                        rank=dt.rank,
                        points=dt.points,
                    )
                )
    return records


def bp_speaker_tab_rows(session: Session, tournament_id: int) -> tuple[int, dict[int, dict]]:
    """Raw per-debater tab entries, keyed by debater_id: {average, stdev, speeches, rank}.
    `rank` is `None` for debaters below the speech minimum."""
    prelim_round_count = session.exec(
        select(func.count())
        .select_from(Round)
        .where(Round.tournament_id == tournament_id, Round.isElimination == False)  # noqa: E712
    ).one()
    minimum_speeches = math.ceil(prelim_round_count / 2) if prelim_round_count else 0

    scores = bp_round_scores(session, tournament_id, elimination=False)
    entries: dict[int, dict] = {}
    qualifying_avg: dict[int, float] = {}
    for debater_id, rounds in scores.items():
        values = list(rounds.values())
        if not values:
            continue
        average, stdev = core.mean_and_stdev(values)
        entries[debater_id] = {
            "average": average,
            "stdev": stdev,
            "speeches": len(values),
            "rank": None,
        }
        if len(values) >= minimum_speeches:
            qualifying_avg[debater_id] = average

    for debater_id, rank in core.competition_ranks(qualifying_avg).items():
        entries[debater_id]["rank"] = rank

    return minimum_speeches, entries


def bp_speaker_tab(session: Session, tournament_id: int) -> BPSpeakerTab:
    minimum_speeches, entries = bp_speaker_tab_rows(session, tournament_id)
    qualifying_ids = [d for d, e in entries.items() if e["rank"] is not None]
    names_teams = tournament_stats._debater_names_and_teams(session, tournament_id, qualifying_ids)

    rows = [
        BPSpeakerTabRow(
            rank=entries[d]["rank"],
            debater_id=d,
            name=names_teams[d][0],
            team_id=names_teams[d][1],
            team_name=names_teams[d][2],
            average=round(entries[d]["average"], 4),
            speeches=entries[d]["speeches"],
            stdev=round(entries[d]["stdev"], 4),
        )
        for d in qualifying_ids
    ]
    rows.sort(key=lambda r: (-r.average, r.name))
    return BPSpeakerTab(minimum_speeches=minimum_speeches, rows=rows)


def bp_team_standings(session: Session, tournament_id: int) -> list[BPTeamStanding]:
    teams = session.exec(
        select(Team, Institution.name)
        .join(Institution, Team.institution_id == Institution.id, isouter=True)
        .where(Team.tournament_id == tournament_id)
    ).all()
    if not teams:
        return []
    team_ids = [t.id for t, _ in teams]

    members_by_team: dict[int, list[int]] = defaultdict(list)
    for tm in session.exec(select(TeamMember).where(TeamMember.team_id.in_(team_ids))).all():
        members_by_team[tm.team_id].append(tm.debater_id)

    prelim_points: dict[int, int] = defaultdict(int)
    elim_points: dict[int, int] = defaultdict(int)
    elim_advances: dict[int, int] = defaultdict(int)
    elim_eliminations: dict[int, int] = defaultdict(int)
    rank_counts: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    debate_teams = session.exec(
        select(BPDebateTeam, Round.isElimination)
        .join(BPDebate, BPDebateTeam.bp_debate_id == BPDebate.id)
        .join(Round, BPDebate.round_id == Round.id)
        .where(BPDebateTeam.team_id.in_(team_ids))
    ).all()
    for dt, is_elim in debate_teams:
        if is_elim and dt.advanced is not None:
            if dt.advanced:
                elim_advances[dt.team_id] += 1
            else:
                elim_eliminations[dt.team_id] += 1
        if dt.points is None:
            continue
        if is_elim:
            elim_points[dt.team_id] += dt.points
        else:
            prelim_points[dt.team_id] += dt.points
            if dt.rank is not None:
                rank_counts[dt.team_id][dt.rank] += 1

    prelim_scores = bp_round_scores(session, tournament_id, elimination=False)
    speaks: dict[int, list[float]] = defaultdict(list)
    for team_id in team_ids:
        for debater_id in members_by_team.get(team_id, []):
            speaks[team_id].extend(prelim_scores.get(debater_id, {}).values())

    standings: list[BPTeamStanding] = []
    # WUDC precedence is (points, speaks); points differences are small integers, so
    # scaling them well above any plausible total-speaks sum lets one competition_ranks
    # call encode both tiers in a single sortable value.
    ranking_value_by_team: dict[int, float] = {}
    for team, institution_name in teams:
        values = speaks.get(team.id, [])
        total_speaks = sum(values)
        average_speaks = (total_speaks / len(values)) if values else 0.0
        counts = rank_counts.get(team.id, {})
        points = prelim_points.get(team.id, 0)
        ranking_value_by_team[team.id] = points * 1_000_000 + total_speaks
        standings.append(
            BPTeamStanding(
                rank=0,
                team_id=team.id,
                name=team.name,
                institution=institution_name,
                prelim_points=points,
                elim_points=elim_points.get(team.id, 0),
                elim_advances=elim_advances.get(team.id, 0),
                elim_eliminations=elim_eliminations.get(team.id, 0),
                firsts=counts.get(1, 0),
                seconds=counts.get(2, 0),
                thirds=counts.get(3, 0),
                fourths=counts.get(4, 0),
                total_speaks=round(total_speaks, 4),
                average_speaks=round(average_speaks, 4),
            )
        )

    ranks = core.competition_ranks(ranking_value_by_team)
    for standing in standings:
        standing.rank = ranks[standing.team_id]
    standings.sort(key=lambda s: (s.rank, s.name))
    return standings


def bp_tournament_summary(session: Session, tournament_id: int) -> BPTournamentSummary:
    teams = session.exec(select(Team).where(Team.tournament_id == tournament_id)).all()
    team_ids = [t.id for t in teams]
    institution_ids = {t.institution_id for t in teams if t.institution_id is not None}

    debater_ids: set[int] = set()
    if team_ids:
        for tm in session.exec(select(TeamMember).where(TeamMember.team_id.in_(team_ids))).all():
            debater_ids.add(tm.debater_id)

    rounds = session.exec(select(Round).where(Round.tournament_id == tournament_id)).all()
    prelim_rounds = sum(1 for r in rounds if not r.isElimination)
    elim_rounds = sum(1 for r in rounds if r.isElimination)

    debates = session.exec(
        select(BPDebate).join(Round, BPDebate.round_id == Round.id).where(Round.tournament_id == tournament_id)
    ).all()
    debate_ids = [d.id for d in debates]

    ballots = (
        session.exec(select(BPBallot).where(BPBallot.bp_debate_id.in_(debate_ids))).all()
        if debate_ids
        else []
    )
    judge_ids = {b.judge_id for b in ballots}
    counted_ballot_ids = [b.id for b in ballots if not b.discarded and not b.forfeit]

    scores = (
        session.exec(
            select(BPSpeakerScore).where(BPSpeakerScore.bp_ballot_id.in_(counted_ballot_ids))
        ).all()
        if counted_ballot_ids
        else []
    )
    mean_score, stdev_score = core.mean_and_stdev([s.final_score for s in scores])

    return BPTournamentSummary(
        tournament_id=tournament_id,
        institutions=len(institution_ids),
        teams=len(teams),
        debaters=len(debater_ids),
        judges=len(judge_ids),
        rounds=len(rounds),
        prelim_rounds=prelim_rounds,
        elim_rounds=elim_rounds,
        debates=len(debates),
        ballots=len(ballots),
        speaker_scores=len(scores),
        mean_speaker_score=round(mean_score, 4) if mean_score is not None else None,
        speaker_score_stdev=round(stdev_score, 4) if stdev_score is not None else None,
    )


def _side_point_stats(rows: list[BPDebateTeam]) -> list[BPSidePointStat]:
    points_by_side: dict[BPSide, list[int]] = defaultdict(list)
    counts_by_side: dict[BPSide, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for dt in rows:
        if dt.points is not None:
            points_by_side[dt.side].append(dt.points)
        if dt.rank is not None:
            counts_by_side[dt.side][dt.rank] += 1

    result = []
    for side in BPSide:
        points = points_by_side.get(side, [])
        counts = counts_by_side.get(side, {})
        average = sum(points) / len(points) if points else None
        result.append(
            BPSidePointStat(
                side=side,
                average_points=round(average, 4) if average is not None else None,
                firsts=counts.get(1, 0),
                seconds=counts.get(2, 0),
                thirds=counts.get(3, 0),
                fourths=counts.get(4, 0),
            )
        )
    return result


def bp_side_stats(session: Session, tournament_id: int) -> BPSideStats:
    rows = session.exec(
        select(BPDebateTeam, Round.id, Round.seq, Round.name)
        .join(BPDebate, BPDebateTeam.bp_debate_id == BPDebate.id)
        .join(Round, BPDebate.round_id == Round.id)
        .where(Round.tournament_id == tournament_id)
    ).all()

    overall = _side_point_stats([dt for dt, _rid, _seq, _name in rows])

    by_round_rows: dict[int, dict] = {}
    for dt, round_id, round_seq, round_name in rows:
        entry = by_round_rows.setdefault(
            round_id, {"round_seq": round_seq, "round_name": round_name, "rows": []}
        )
        entry["rows"].append(dt)

    by_round = [
        BPRoundSideStats(
            round_id=round_id,
            round_seq=entry["round_seq"],
            round_name=entry["round_name"],
            sides=_side_point_stats(entry["rows"]),
        )
        for round_id, entry in sorted(by_round_rows.items(), key=lambda kv: kv[1]["round_seq"])
    ]

    return BPSideStats(overall=overall, by_round=by_round)


def bp_motion_stats(session: Session, tournament_id: int) -> list[BPMotionStat]:
    motions = session.exec(
        select(Motion, Round).join(Round, Motion.round_id == Round.id).where(Round.tournament_id == tournament_id)
    ).all()
    if not motions:
        return []

    results = []
    for motion, round_ in motions:
        debates = session.exec(select(BPDebate).where(BPDebate.round_id == round_.id)).all()
        debate_ids = [d.id for d in debates]

        points_by_side: dict[BPSide, list[int]] = defaultdict(list)
        speaks_by_side: dict[BPSide, list[float]] = defaultdict(list)
        if debate_ids:
            for dt in session.exec(
                select(BPDebateTeam).where(BPDebateTeam.bp_debate_id.in_(debate_ids))
            ).all():
                if dt.points is not None:
                    points_by_side[dt.side].append(dt.points)

            score_rows = session.exec(
                select(BPSpeakerScore.side, BPSpeakerScore.final_score)
                .join(BPBallot, BPSpeakerScore.bp_ballot_id == BPBallot.id)
                .where(
                    BPBallot.bp_debate_id.in_(debate_ids),
                    BPBallot.discarded == False,  # noqa: E712
                    BPBallot.forfeit == False,  # noqa: E712
                )
            ).all()
            for side, final_score in score_rows:
                speaks_by_side[side].append(final_score)

        sides = []
        for side in BPSide:
            points = points_by_side.get(side, [])
            speaks = speaks_by_side.get(side, [])
            sides.append(
                BPMotionSideStat(
                    side=side,
                    average_points=round(sum(points) / len(points), 4) if points else None,
                    average_speaks=round(sum(speaks) / len(speaks), 4) if speaks else None,
                )
            )

        results.append(
            BPMotionStat(
                motion_id=motion.id,
                round_id=round_.id,
                round_seq=round_.seq,
                text=motion.text,
                sides=sides,
            )
        )
    results.sort(key=lambda m: m.round_seq)
    return results
