"""A tournament's computed tab, shared by the tournament, debater and school pages.

`tournament_snapshot` is the one place the speaker tab (unranked rows included), standings,
brackets and per-team finishes are worked out, and it is cached as plain values.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.models import Debate, Debater, Motion, Round, Side, Team, TeamMember, Tournament
from app.services.public import cache
from app.services.public.brackets import Bracket, ElimDebate, ElimRound, derive_brackets
from app.services.public.loaders import person_name, tournament_ref
from app.services.public.schemas import TournamentRef
from app.services.stats import tournament as tournament_stats


@dataclass
class TeamInfo:
    id: int
    name: str
    institution_id: int | None
    debater_ids: list[int] = field(default_factory=list)


@dataclass
class StandingEntry:
    rank: int
    team_id: int
    prelim_wins: int
    prelim_losses: int
    elim_wins: int
    elim_losses: int
    total_speaks: float
    average_speaks: float


@dataclass
class TabEntry:
    debater_id: int
    name: str
    average: float
    stdev: float
    speeches: int
    rank: int | None  # None below the speech minimum
    team_id: int | None


@dataclass
class RoundInfo:
    id: int
    seq: int
    name: str | None
    abbr: str | None
    is_elimination: bool
    motion: str | None
    info_slide: str | None
    debates: int
    prop_wins: int
    opp_wins: int
    average_prop_speaks: float | None
    average_opp_speaks: float | None


@dataclass
class TeamFinish:
    finish: str  # champion / finalist / semi / quarter / octo / broke
    bracket_label: str
    primary: bool


@dataclass
class TournamentSnapshot:
    tournament: TournamentRef
    teams: dict[int, TeamInfo]
    standings: list[StandingEntry]
    standing_by_team: dict[int, StandingEntry]
    minimum_speeches: int
    tab: list[TabEntry]  # every debater with a prelim score, ranked rows first by average
    tab_by_debater: dict[int, TabEntry]
    debater_team: dict[int, int]
    brackets: list[Bracket]
    finishes: dict[int, TeamFinish]
    rounds: list[RoundInfo]
    debater_names: dict[int, str]


def elim_rounds_by_tournament(
    session: Session, tournament_id: int | None = None
) -> dict[int, list[ElimRound]]:
    """Every elimination round with its debates, grouped by tournament, in one query."""
    statement = (
        select(
            Round.tournament_id,
            Round.id,
            Round.seq,
            Round.name,
            Debate.id,
            Debate.prop_team_id,
            Debate.opp_team_id,
            Debate.winner,
        )
        .join(Debate, Debate.round_id == Round.id)
        .where(Round.isElimination == True)  # noqa: E712
        .order_by(Round.tournament_id, Round.seq, Debate.id)
    )
    if tournament_id is not None:
        statement = statement.where(Round.tournament_id == tournament_id)
    rounds: dict[int, dict[int, ElimRound]] = defaultdict(dict)
    for tid, round_id, seq, name, debate_id, prop, opp, winner in session.exec(statement).all():
        elim_round = rounds[tid].setdefault(
            round_id, ElimRound(round_id=round_id, seq=seq, name=name, debates=[])
        )
        winner_team = None if winner is None else (prop if winner == Side.PROP else opp)
        elim_round.debates.append(
            ElimDebate(
                debate_id=debate_id, prop_team_id=prop, opp_team_id=opp, winner_team_id=winner_team
            )
        )
    return {tid: list(by_round.values()) for tid, by_round in rounds.items()}


def team_finishes(brackets: list[Bracket]) -> dict[int, TeamFinish]:
    return {
        team_id: TeamFinish(finish=finish, bracket_label=bracket.label, primary=bracket.primary)
        for bracket in brackets
        for team_id, finish in bracket.finishes.items()
    }


def _build(session: Session, tournament_id: int) -> TournamentSnapshot | None:
    tournament = session.get(Tournament, tournament_id)
    if tournament is None:
        return None

    teams = {
        t.id: TeamInfo(id=t.id, name=t.name, institution_id=t.institution_id)
        for t in session.exec(select(Team).where(Team.tournament_id == tournament_id)).all()
    }
    debater_team: dict[int, int] = {}
    members = session.exec(
        select(TeamMember.team_id, TeamMember.debater_id)
        .join(Team, TeamMember.team_id == Team.id)
        .where(Team.tournament_id == tournament_id)
        .order_by(TeamMember.id)
    ).all()
    for team_id, debater_id in members:
        teams[team_id].debater_ids.append(debater_id)
        debater_team.setdefault(debater_id, team_id)

    standings = [
        StandingEntry(
            rank=s.rank,
            team_id=s.team_id,
            prelim_wins=s.prelim_wins,
            prelim_losses=s.prelim_losses,
            elim_wins=s.elim_wins,
            elim_losses=s.elim_losses,
            total_speaks=s.total_speaks,
            average_speaks=s.average_speaks,
        )
        for s in tournament_stats.team_standings(session, tournament_id)
    ]

    minimum_speeches, entries = tournament_stats.speaker_tab_rows(session, tournament_id)
    names = {
        d.id: person_name(d, "Debater")
        for d in session.exec(
            select(Debater).where(Debater.id.in_(list(entries) or [-1]))
        ).all()
    }
    tab = [
        TabEntry(
            debater_id=debater_id,
            name=names.get(debater_id, ""),
            average=round(entry["average"], 4),
            stdev=round(entry["stdev"], 4),
            speeches=entry["speeches"],
            rank=entry["rank"],
            team_id=debater_team.get(debater_id),
        )
        for debater_id, entry in entries.items()
    ]
    tab.sort(key=lambda e: (e.rank is None, e.rank or 0, -e.average, e.name))

    brackets = derive_brackets(elim_rounds_by_tournament(session, tournament_id).get(tournament_id, []))

    motion_stats = {m.round_id: m for m in tournament_stats.motion_stats(session, tournament_id)}
    infos = {
        m.round_id: m.info_slide
        for m in session.exec(
            select(Motion).join(Round, Motion.round_id == Round.id).where(Round.tournament_id == tournament_id)
        ).all()
    }
    debate_counts: dict[int, int] = defaultdict(int)
    round_wins: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for round_id, winner in session.exec(
        select(Debate.round_id, Debate.winner)
        .join(Round, Debate.round_id == Round.id)
        .where(Round.tournament_id == tournament_id)
    ).all():
        debate_counts[round_id] += 1
        if winner == Side.PROP:
            round_wins[round_id][0] += 1
        elif winner == Side.OPP:
            round_wins[round_id][1] += 1
    rounds = []
    for round_ in session.exec(
        select(Round).where(Round.tournament_id == tournament_id).order_by(Round.seq)
    ).all():
        stat = motion_stats.get(round_.id)
        rounds.append(
            RoundInfo(
                id=round_.id,
                seq=round_.seq,
                name=round_.name,
                abbr=round_.abbr,
                is_elimination=round_.isElimination,
                motion=stat.text if stat else None,
                info_slide=infos.get(round_.id),
                debates=debate_counts.get(round_.id, 0),
                prop_wins=round_wins[round_.id][0] if round_.id in round_wins else 0,
                opp_wins=round_wins[round_.id][1] if round_.id in round_wins else 0,
                average_prop_speaks=stat.average_prop_speaks if stat else None,
                average_opp_speaks=stat.average_opp_speaks if stat else None,
            )
        )

    return TournamentSnapshot(
        tournament=tournament_ref(tournament),
        teams=teams,
        standings=standings,
        standing_by_team={s.team_id: s for s in standings},
        minimum_speeches=minimum_speeches,
        tab=tab,
        tab_by_debater={e.debater_id: e for e in tab},
        debater_team=debater_team,
        brackets=brackets,
        finishes=team_finishes(brackets),
        rounds=rounds,
        debater_names=names,
    )


def tournament_snapshot(session: Session, tournament_id: int) -> TournamentSnapshot | None:
    return cache.cached(("tournament", tournament_id), lambda: _build(session, tournament_id))
