"""Load-bearing statistical primitives shared by the stats service family.

Everything else in `app.services.stats` is built on `round_scores`, `participation`,
`competition_ranks`, `percentile_ranks` and `mean_and_stdev`. Get these right first.
"""

from collections import defaultdict
from dataclasses import dataclass
from statistics import pstdev
from typing import TypeVar

from sqlmodel import Session, select

from app.models import Ballot, Debate, Round, Side, SpeakerPosition, SpeakerScore, TeamMember

K = TypeVar("K")


@dataclass
class Participation:
    """One debater's participation in one debate."""

    debater_id: int
    tournament_id: int
    round_id: int
    round_seq: int
    is_elimination: bool
    debate_id: int
    team_id: int
    side: Side
    won: bool | None  # None when the debate has no recorded winner


def _round_position_averages(
    session: Session, tournament_id: int, *, elimination: bool
) -> dict[int, dict[int, dict[int, float]]]:
    """{debater_id: {round_id: {position: panel-averaged score}}}, replies already dropped."""
    statement = (
        select(SpeakerScore, Debate.round_id)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .join(Debate, Ballot.debate_id == Debate.id)
        .join(Round, Debate.round_id == Round.id)
        .where(
            Round.tournament_id == tournament_id,
            Round.isElimination == elimination,
            Ballot.discarded == False,  # noqa: E712
            Ballot.forfeit == False,  # noqa: E712
        )
    )
    rows = session.exec(statement).all()

    panel_buckets: dict[tuple[int, int, int], list[float]] = defaultdict(list)
    for score, round_id in rows:
        if score.position == SpeakerPosition.REPLY:
            continue
        panel_buckets[(score.debater_id, round_id, score.position)].append(score.final_score)

    result: dict[int, dict[int, dict[int, float]]] = defaultdict(lambda: defaultdict(dict))
    for (debater_id, round_id, position), values in panel_buckets.items():
        result[debater_id][round_id][position] = sum(values) / len(values)
    return {d: dict(rounds) for d, rounds in result.items()}


def round_scores_with_iron(
    session: Session, tournament_id: int, *, elimination: bool = False
) -> dict[int, dict[int, tuple[float, bool]]]:
    """Like `round_scores` but also flags whether the round score came from an iron speech
    (the debater spoke more than one substantive position that round)."""
    position_averages = _round_position_averages(session, tournament_id, elimination=elimination)
    result: dict[int, dict[int, tuple[float, bool]]] = {}
    for debater_id, rounds in position_averages.items():
        result[debater_id] = {
            round_id: (max(positions.values()), len(positions) > 1)
            for round_id, positions in rounds.items()
        }
    return result


def round_scores(
    session: Session, tournament_id: int, *, elimination: bool = False
) -> dict[int, dict[int, float]]:
    """{debater_id: {round_id: score}} with v1's three normalizations applied:
    panel merge (mean across judges), reply drop, and iron merge (max over positions spoken)."""
    detailed = round_scores_with_iron(session, tournament_id, elimination=elimination)
    return {
        debater_id: {round_id: score for round_id, (score, _is_iron) in rounds.items()}
        for debater_id, rounds in detailed.items()
    }


def participation(session: Session, tournament_id: int | None = None) -> list[Participation]:
    """Per-debate side/outcome records. A debater participated in a debate if they have a
    SpeakerScore row for it (from a non-discarded, non-forfeit ballot); debates with no
    recorded speaker scores at all fall back to full TeamMember roster membership."""
    debate_query = select(Debate, Round).join(Round, Debate.round_id == Round.id)
    if tournament_id is not None:
        debate_query = debate_query.where(Round.tournament_id == tournament_id)
    debates = session.exec(debate_query).all()
    if not debates:
        return []

    team_ids = {d.prop_team_id for d, _ in debates} | {d.opp_team_id for d, _ in debates}
    members_by_team: dict[int, set[int]] = defaultdict(set)
    for tm in session.exec(select(TeamMember).where(TeamMember.team_id.in_(team_ids))).all():
        members_by_team[tm.team_id].add(tm.debater_id)

    debate_ids = [d.id for d, _ in debates]
    scored_by_debate: dict[int, set[int]] = defaultdict(set)
    score_rows = session.exec(
        select(SpeakerScore.debater_id, Ballot.debate_id)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .where(
            Ballot.debate_id.in_(debate_ids),
            Ballot.discarded == False,  # noqa: E712
            Ballot.forfeit == False,  # noqa: E712
        )
    ).all()
    for debater_id, debate_id in score_rows:
        scored_by_debate[debate_id].add(debater_id)

    records: list[Participation] = []
    for debate, round_ in debates:
        scored = scored_by_debate.get(debate.id, set())
        if scored:
            prop_debaters = scored & members_by_team.get(debate.prop_team_id, set())
            opp_debaters = scored & members_by_team.get(debate.opp_team_id, set())
        else:
            prop_debaters = members_by_team.get(debate.prop_team_id, set())
            opp_debaters = members_by_team.get(debate.opp_team_id, set())

        for side, team_id, debater_ids in (
            (Side.PROP, debate.prop_team_id, prop_debaters),
            (Side.OPP, debate.opp_team_id, opp_debaters),
        ):
            won = None if debate.winner is None else debate.winner == side
            for debater_id in debater_ids:
                records.append(
                    Participation(
                        debater_id=debater_id,
                        tournament_id=round_.tournament_id,
                        round_id=round_.id,
                        round_seq=round_.seq,
                        is_elimination=round_.isElimination,
                        debate_id=debate.id,
                        team_id=team_id,
                        side=side,
                        won=won,
                    )
                )
    return records


def competition_ranks(values: dict[K, float]) -> dict[K, int]:
    """Descending competition ranking; ties share a rank, gaps follow (1, 2, 2, 4).
    Compares on `round(x, 4)` rather than exact equality."""
    rounded = sorted(values.items(), key=lambda kv: round(kv[1], 4), reverse=True)
    ranks: dict[K, int] = {}
    prev_value: float | None = None
    prev_rank = 0
    for index, (key, value) in enumerate(rounded, start=1):
        rounded_value = round(value, 4)
        if rounded_value == prev_value:
            ranks[key] = prev_rank
        else:
            ranks[key] = index
            prev_rank = index
            prev_value = rounded_value
    return ranks


def percentile_ranks(values: dict[K, float]) -> dict[K, float]:
    """Cumulative percentage of the population <= x, matching Apache Commons Math's
    `Frequency.getCumPct(x) * 100`."""
    if not values:
        return {}
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    result: dict[K, float] = {}
    i = 0
    while i < n:
        j = i
        while j < n and items[j][1] == items[i][1]:
            j += 1
        pct = (j / n) * 100
        for k in range(i, j):
            result[items[k][0]] = pct
        i = j
    return result


def mean_and_stdev(values: list[float]) -> tuple[float | None, float | None]:
    """Population mean and standard deviation. `(None, None)` for an empty population."""
    if not values:
        return None, None
    return sum(values) / len(values), pstdev(values)


def display_name(full_name: str | None, first_name: str | None, last_name: str | None) -> str:
    if full_name:
        return full_name
    parts = [p for p in (first_name, last_name) if p]
    return " ".join(parts)
