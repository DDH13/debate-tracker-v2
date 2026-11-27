"""Elimination brackets, derived from which teams carry over between elimination rounds.

Tabbycat (and this app) store elimination rounds as a flat list: there is no bracket id. Real
tournaments run several brackets side by side (an open bracket and a novice bracket) and the
last elimination round is not always the grand final (apiit isdc runs "Novice Semis" and
"Novice Finals" *after* the Grand Final). So rounds are grouped by overlap: a round joins the
open bracket whose still-alive teams appear in it, otherwise it starts a new bracket.

Pure: no database access. `derive_brackets` takes plain dataclasses.
"""

from dataclasses import dataclass, field
from typing import Literal

Finish = Literal["champion", "finalist", "semi", "quarter", "octo", "broke"]

FINISH_BY_DEPTH: dict[int, Finish] = {0: "finalist", 1: "semi", 2: "quarter", 3: "octo"}


@dataclass
class ElimDebate:
    debate_id: int
    prop_team_id: int
    opp_team_id: int
    winner_team_id: int | None = None


@dataclass
class ElimRound:
    round_id: int
    seq: int
    name: str | None
    debates: list[ElimDebate]


@dataclass
class Bracket:
    label: str
    rounds: list[ElimRound] = field(default_factory=list)
    champion_team_id: int | None = None
    finalist_team_id: int | None = None
    # team_id -> how far it went in this bracket
    finishes: dict[int, Finish] = field(default_factory=dict)
    team_ids: set[int] = field(default_factory=set)
    primary: bool = False

    def alive(self) -> set[int]:
        """Teams that go on to the next round: the last round's winners, or everyone in it
        while any of its debates is undecided."""
        last = self.rounds[-1]
        if any(d.winner_team_id is None for d in last.debates):
            return {t for d in last.debates for t in (d.prop_team_id, d.opp_team_id)}
        return {d.winner_team_id for d in last.debates if d.winner_team_id is not None}


def _teams(round_: ElimRound) -> set[int]:
    return {t for d in round_.debates for t in (d.prop_team_id, d.opp_team_id)}


def _depth_of_last_round(round_: ElimRound) -> int:
    """1 debate is the final (0), 2 semis (1), 3-4 quarters (2), 5-8 octos (3)..."""
    depth = 0
    capacity = 1
    while capacity < len(round_.debates):
        capacity *= 2
        depth += 1
    return depth


def _finish(depth: int) -> Finish:
    return FINISH_BY_DEPTH.get(depth, "broke")


def _resolve(bracket: Bracket) -> None:
    rounds = bracket.rounds
    bracket.label = rounds[-1].name or f"Round {rounds[-1].seq}"
    bracket.team_ids = set().union(*(_teams(r) for r in rounds))
    last_depth = _depth_of_last_round(rounds[-1])
    last_index = len(rounds) - 1

    last_round_of: dict[int, int] = {}
    for index, round_ in enumerate(rounds):
        for team_id in _teams(round_):
            last_round_of[team_id] = index

    for team_id, index in last_round_of.items():
        depth = last_depth + (last_index - index)
        bracket.finishes[team_id] = _finish(depth)

    final = rounds[-1]
    if last_depth == 0 and len(final.debates) == 1:
        debate = final.debates[0]
        if debate.winner_team_id is not None:
            loser = (
                debate.opp_team_id
                if debate.winner_team_id == debate.prop_team_id
                else debate.prop_team_id
            )
            bracket.champion_team_id = debate.winner_team_id
            bracket.finalist_team_id = loser
            bracket.finishes[debate.winner_team_id] = "champion"
            bracket.finishes[loser] = "finalist"
            return
    # No final decided yet: the last round's winners have won through to the next stage.
    if last_depth > 0:
        for debate in final.debates:
            if debate.winner_team_id is not None:
                bracket.finishes[debate.winner_team_id] = _finish(last_depth - 1)


def derive_brackets(elim_rounds: list[ElimRound]) -> list[Bracket]:
    """Brackets in order of first appearance, with the largest marked `primary`.

    `finishes` maps each team to its depth from that bracket's final: champion, finalist, semi,
    quarter, octo, or "broke" for anything deeper. A bracket whose last round is not a single
    decided debate has no champion."""
    brackets: list[Bracket] = []
    for round_ in sorted(elim_rounds, key=lambda r: r.seq):
        if not round_.debates:
            continue
        teams = _teams(round_)
        best: Bracket | None = None
        best_overlap = 0
        for bracket in brackets:
            # A bracket is never closed up front: a single decided debate may be a play-in
            # before byes ("Partial Octos"), and it only ends when no later round carries its
            # winners on (a novice bracket after the grand final shares no team with it).
            overlap = len(bracket.alive() & teams)
            if overlap > best_overlap:
                best, best_overlap = bracket, overlap
        if best is None:
            best = Bracket(label="")
            brackets.append(best)
        best.rounds.append(round_)

    for bracket in brackets:
        _resolve(bracket)
    if brackets:
        max(brackets, key=lambda b: len(b.team_ids)).primary = True
    return brackets
