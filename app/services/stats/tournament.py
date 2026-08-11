"""Tournament-scoped statistics: speaker tab, team standings, summary, side and motion stats.

These touch only one tournament's rows (~3.5k for a typical tournament) so they are computed
fresh on every request rather than materialized.
"""

import math
from collections import defaultdict

from sqlmodel import Session, SQLModel, func, select

from app.models import (
    Ballot,
    Debate,
    Debater,
    Institution,
    Motion,
    Round,
    Side,
    SpeakerScore,
    Team,
    TeamMember,
)
from app.services.stats import core


class SpeakerTabRow(SQLModel):
    rank: int
    debater_id: int
    name: str
    team_id: int | None
    team_name: str | None
    average: float
    speeches: int
    stdev: float


class SpeakerTab(SQLModel):
    minimum_speeches: int
    rows: list[SpeakerTabRow]


class TeamStanding(SQLModel):
    rank: int
    team_id: int
    name: str
    institution: str | None
    prelim_wins: int
    prelim_losses: int
    elim_wins: int
    elim_losses: int
    total_speaks: float
    average_speaks: float


class TournamentSummary(SQLModel):
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
    prop_wins: int
    opp_wins: int
    prop_win_rate: float | None
    average_content: float | None
    average_style: float | None
    average_strategy: float | None


class SideWinStat(SQLModel):
    prop_wins: int
    opp_wins: int
    prop_win_rate: float | None


class RoundSideStat(SideWinStat):
    round_id: int
    round_seq: int
    round_name: str | None


class SideStats(SQLModel):
    overall: SideWinStat
    by_round: list[RoundSideStat]


class MotionStat(SQLModel):
    motion_id: int
    round_id: int
    round_seq: int
    text: str
    prop_wins: int
    opp_wins: int
    prop_win_rate: float | None
    average_prop_speaks: float | None
    average_opp_speaks: float | None


def speaker_tab_rows(session: Session, tournament_id: int) -> tuple[int, dict[int, dict]]:
    """Raw per-debater tab entries, keyed by debater_id: {average, stdev, speeches, rank}.
    `rank` is `None` for debaters below the speech minimum. Shared with the debater profile
    builder so a tournament's tab is only computed once."""
    prelim_round_count = session.exec(
        select(func.count())
        .select_from(Round)
        .where(Round.tournament_id == tournament_id, Round.isElimination == False)  # noqa: E712
    ).one()
    minimum_speeches = math.ceil(prelim_round_count / 2) if prelim_round_count else 0

    scores = core.round_scores(session, tournament_id, elimination=False)
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


def _debater_names_and_teams(
    session: Session, tournament_id: int, debater_ids: list[int]
) -> dict[int, tuple[str, int | None, str | None]]:
    if not debater_ids:
        return {}
    debaters = {
        d.id: d for d in session.exec(select(Debater).where(Debater.id.in_(debater_ids))).all()
    }
    team_by_debater: dict[int, tuple[int, str]] = {}
    rows = session.exec(
        select(TeamMember.debater_id, Team.id, Team.name)
        .join(Team, TeamMember.team_id == Team.id)
        .where(TeamMember.debater_id.in_(debater_ids), Team.tournament_id == tournament_id)
    ).all()
    for debater_id, team_id, team_name in rows:
        team_by_debater.setdefault(debater_id, (team_id, team_name))

    result = {}
    for debater_id in debater_ids:
        debater = debaters.get(debater_id)
        name = (
            core.display_name(debater.full_name, debater.first_name, debater.last_name)
            if debater
            else ""
        )
        team_id, team_name = team_by_debater.get(debater_id, (None, None))
        result[debater_id] = (name, team_id, team_name)
    return result


def speaker_tab(session: Session, tournament_id: int) -> SpeakerTab:
    minimum_speeches, entries = speaker_tab_rows(session, tournament_id)
    qualifying_ids = [d for d, e in entries.items() if e["rank"] is not None]
    names_teams = _debater_names_and_teams(session, tournament_id, qualifying_ids)

    rows = [
        SpeakerTabRow(
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
    return SpeakerTab(minimum_speeches=minimum_speeches, rows=rows)


def team_standings(session: Session, tournament_id: int) -> list[TeamStanding]:
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

    win_loss: dict[int, dict[str, int]] = defaultdict(
        lambda: {"prelim_wins": 0, "prelim_losses": 0, "elim_wins": 0, "elim_losses": 0}
    )
    debates = session.exec(
        select(Debate, Round.isElimination)
        .join(Round, Debate.round_id == Round.id)
        .where(Round.tournament_id == tournament_id)
    ).all()
    for debate, is_elim in debates:
        if debate.winner is None:
            continue
        prefix = "elim" if is_elim else "prelim"
        winning_team = debate.prop_team_id if debate.winner == Side.PROP else debate.opp_team_id
        losing_team = debate.opp_team_id if debate.winner == Side.PROP else debate.prop_team_id
        win_loss[winning_team][f"{prefix}_wins"] += 1
        win_loss[losing_team][f"{prefix}_losses"] += 1

    prelim_scores = core.round_scores(session, tournament_id, elimination=False)
    speaks: dict[int, list[float]] = defaultdict(list)
    for team_id in team_ids:
        for debater_id in members_by_team.get(team_id, []):
            speaks[team_id].extend(prelim_scores.get(debater_id, {}).values())

    standings: list[TeamStanding] = []
    average_by_team: dict[int, float] = {}
    empty_wl = {"prelim_wins": 0, "prelim_losses": 0, "elim_wins": 0, "elim_losses": 0}
    for team, institution_name in teams:
        values = speaks.get(team.id, [])
        total = sum(values)
        average = (total / len(values)) if values else 0.0
        average_by_team[team.id] = average
        wl = win_loss.get(team.id, empty_wl)
        standings.append(
            TeamStanding(
                rank=0,
                team_id=team.id,
                name=team.name,
                institution=institution_name,
                prelim_wins=wl["prelim_wins"],
                prelim_losses=wl["prelim_losses"],
                elim_wins=wl["elim_wins"],
                elim_losses=wl["elim_losses"],
                total_speaks=round(total, 4),
                average_speaks=round(average, 4),
            )
        )

    ranks = core.competition_ranks(average_by_team)
    for standing in standings:
        standing.rank = ranks[standing.team_id]
    standings.sort(key=lambda s: (s.rank, s.name))
    return standings


def tournament_summary(session: Session, tournament_id: int) -> TournamentSummary:
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
        select(Debate)
        .join(Round, Debate.round_id == Round.id)
        .where(Round.tournament_id == tournament_id)
    ).all()
    debate_ids = [d.id for d in debates]

    ballots = (
        session.exec(select(Ballot).where(Ballot.debate_id.in_(debate_ids))).all()
        if debate_ids
        else []
    )
    judge_ids = {b.judge_id for b in ballots}
    counted_ballot_ids = [b.id for b in ballots if not b.discarded and not b.forfeit]

    scores = (
        session.exec(select(SpeakerScore).where(SpeakerScore.ballot_id.in_(counted_ballot_ids))).all()
        if counted_ballot_ids
        else []
    )
    mean_score, stdev_score = core.mean_and_stdev([s.final_score for s in scores])
    content_scores = [s.content for s in scores if s.content is not None]
    style_scores = [s.style for s in scores if s.style is not None]
    strategy_scores = [s.strategy for s in scores if s.strategy is not None]

    prop_wins = sum(1 for d in debates if d.winner == Side.PROP)
    opp_wins = sum(1 for d in debates if d.winner == Side.OPP)
    decided = prop_wins + opp_wins

    return TournamentSummary(
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
        prop_wins=prop_wins,
        opp_wins=opp_wins,
        prop_win_rate=round(prop_wins / decided * 100, 4) if decided else None,
        average_content=round(sum(content_scores) / len(content_scores), 4) if content_scores else None,
        average_style=round(sum(style_scores) / len(style_scores), 4) if style_scores else None,
        average_strategy=round(sum(strategy_scores) / len(strategy_scores), 4)
        if strategy_scores
        else None,
    )


def side_stats(session: Session, tournament_id: int) -> SideStats:
    rows = session.exec(
        select(Debate, Round.id, Round.seq, Round.name)
        .join(Round, Debate.round_id == Round.id)
        .where(Round.tournament_id == tournament_id)
    ).all()

    overall_prop = overall_opp = 0
    by_round: dict[int, dict] = {}
    for debate, round_id, round_seq, round_name in rows:
        if debate.winner is None:
            continue
        entry = by_round.setdefault(
            round_id,
            {"round_seq": round_seq, "round_name": round_name, "prop_wins": 0, "opp_wins": 0},
        )
        if debate.winner == Side.PROP:
            overall_prop += 1
            entry["prop_wins"] += 1
        else:
            overall_opp += 1
            entry["opp_wins"] += 1

    total = overall_prop + overall_opp
    overall = SideWinStat(
        prop_wins=overall_prop,
        opp_wins=overall_opp,
        prop_win_rate=round(overall_prop / total * 100, 4) if total else None,
    )

    by_round_stats = [
        RoundSideStat(
            round_id=round_id,
            round_seq=entry["round_seq"],
            round_name=entry["round_name"],
            prop_wins=entry["prop_wins"],
            opp_wins=entry["opp_wins"],
            prop_win_rate=round(
                entry["prop_wins"] / (entry["prop_wins"] + entry["opp_wins"]) * 100, 4
            )
            if (entry["prop_wins"] + entry["opp_wins"])
            else None,
        )
        for round_id, entry in sorted(by_round.items(), key=lambda kv: kv[1]["round_seq"])
    ]

    return SideStats(overall=overall, by_round=by_round_stats)


def motion_stats(session: Session, tournament_id: int) -> list[MotionStat]:
    motions = session.exec(
        select(Motion, Round)
        .join(Round, Motion.round_id == Round.id)
        .where(Round.tournament_id == tournament_id)
    ).all()
    if not motions:
        return []

    results = []
    for motion, round_ in motions:
        debates = session.exec(select(Debate).where(Debate.round_id == round_.id)).all()
        prop_wins = sum(1 for d in debates if d.winner == Side.PROP)
        opp_wins = sum(1 for d in debates if d.winner == Side.OPP)
        decided = prop_wins + opp_wins

        debate_ids = [d.id for d in debates]
        prop_scores: list[float] = []
        opp_scores: list[float] = []
        if debate_ids:
            score_rows = session.exec(
                select(SpeakerScore.side, SpeakerScore.final_score)
                .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
                .where(
                    Ballot.debate_id.in_(debate_ids),
                    Ballot.discarded == False,  # noqa: E712
                    Ballot.forfeit == False,  # noqa: E712
                )
            ).all()
            for side, final_score in score_rows:
                (prop_scores if side == Side.PROP else opp_scores).append(final_score)

        results.append(
            MotionStat(
                motion_id=motion.id,
                round_id=round_.id,
                round_seq=round_.seq,
                text=motion.text,
                prop_wins=prop_wins,
                opp_wins=opp_wins,
                prop_win_rate=round(prop_wins / decided * 100, 4) if decided else None,
                average_prop_speaks=round(sum(prop_scores) / len(prop_scores), 4)
                if prop_scores
                else None,
                average_opp_speaks=round(sum(opp_scores) / len(opp_scores), 4)
                if opp_scores
                else None,
            )
        )
    results.sort(key=lambda m: m.round_seq)
    return results
