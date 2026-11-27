"""Judge-side content / style / strategy figures: how a judge marks the three categories.

Only substantive speeches (positions 1-3) on counted ballots (not discarded, not forfeit) take
part, since a reply is on a half scale. Everything is derived from one cached query
(`category_data`), as plain dataclasses, and is never materialized: it is current right after
an import (within the cache TTL). The pure `build_category_data` does the work, so the
arithmetic is testable without a database. A debater's own category figures are built in
`debater.py` from the speeches that page already loads.
"""

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from statistics import pstdev
from typing import Literal

from sqlmodel import Session, SQLModel, select

from app.models import Ballot, SpeakerPosition, SpeakerScore
from app.services.public import cache
from app.services.public.field import MIN_JUDGE_BALLOTS
from app.services.public.schemas import Metric, metric

MIN_SEPARATION_MARKS = 30

Category = Literal["content", "style", "strategy"]
CATEGORIES: tuple[Category, ...] = ("content", "style", "strategy")
SCALES: dict[Category, int] = {"content": 40, "style": 40, "strategy": 20}

Edges = tuple[float, float, float]


@dataclass(frozen=True)
class Mark:
    """One speaker's mark on one ballot, with what the judge figures need."""

    debater_id: int
    position: int
    content: float
    style: float
    strategy: float
    ballot_id: int
    judge_id: int
    side: str
    winner: str  # the ballot's winning side
    discarded: bool = False
    forfeit: bool = False

    @property
    def values(self) -> Edges:
        return (self.content, self.style, self.strategy)


@dataclass
class JudgeFigures:
    ballots: int
    average: Edges
    spread: Edges
    decides: tuple[float | None, float | None, float | None]  # winner minus loser, % of scale
    separation: float | None


@dataclass
class CategoryData:
    judges: dict[int, JudgeFigures] = field(default_factory=dict)
    # Judges with enough ballots: mean of each figure.
    judge_field_average: Edges | None = None
    judge_field_spread: Edges | None = None
    judge_field_decides: tuple[float | None, float | None, float | None] = (None, None, None)
    judge_field_separation: float | None = None


# --- response models


class JudgeCategory(SQLModel):
    category: Category
    scale: int
    average: Metric
    spread: Metric  # standard deviation of their marks: how much of the scale they use
    decides: Metric  # winner minus loser, % of scale


class JudgeCategories(SQLModel):
    ballots: int
    categories: list[JudgeCategory]
    separation: Metric | None = None  # 1 - mean pairwise correlation of the three categories


# --- arithmetic


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def _correlation(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def _counted(marks: Iterable[Mark]) -> list[Mark]:
    return [m for m in marks if m.position != int(SpeakerPosition.REPLY) and not m.discarded and not m.forfeit]


def _judge_figures(marks: list[Mark]) -> dict[int, JudgeFigures]:
    by_judge: dict[int, list[Mark]] = defaultdict(list)
    for m in marks:
        by_judge[m.judge_id].append(m)
    figures: dict[int, JudgeFigures] = {}
    for judge_id, judge_marks in by_judge.items():
        ballots: dict[int, list[Mark]] = defaultdict(list)
        for m in judge_marks:
            ballots[m.ballot_id].append(m)
        columns = [[m.values[i] for m in judge_marks] for i in range(3)]
        gaps: list[list[float]] = [[], [], []]
        for ballot in ballots.values():
            won = [m for m in ballot if m.side == m.winner]
            lost = [m for m in ballot if m.side != m.winner]
            if not won or not lost:
                continue  # a side with no marks: no gap to measure
            for i in range(3):
                gaps[i].append(
                    (sum(m.values[i] for m in won) / len(won) - sum(m.values[i] for m in lost) / len(lost))
                    / SCALES[CATEGORIES[i]]
                    * 100
                )
        separation = None
        if len(judge_marks) >= MIN_SEPARATION_MARKS:
            rs = [
                r
                for a, b in ((0, 1), (0, 2), (1, 2))
                if (r := _correlation(columns[a], columns[b])) is not None
            ]
            separation = 1 - sum(rs) / len(rs) if rs else None
        figures[judge_id] = JudgeFigures(
            ballots=len(ballots),
            average=tuple(sum(c) / len(c) for c in columns),  # type: ignore[arg-type]
            spread=tuple(pstdev(c) for c in columns),  # type: ignore[arg-type]
            decides=tuple(_mean(g) for g in gaps),  # type: ignore[arg-type]
            separation=separation,
        )
    return figures


def build_category_data(marks: Iterable[Mark]) -> CategoryData:
    data = CategoryData(judges=_judge_figures(_counted(marks)))
    qualified = [f for f in data.judges.values() if f.ballots >= MIN_JUDGE_BALLOTS]
    if qualified:
        data.judge_field_average = tuple(_mean(f.average[i] for f in qualified) for i in range(3))  # type: ignore[assignment]
        data.judge_field_spread = tuple(_mean(f.spread[i] for f in qualified) for i in range(3))  # type: ignore[assignment]
        data.judge_field_decides = tuple(  # type: ignore[assignment]
            _mean(f.decides[i] for f in qualified if f.decides[i] is not None) for i in range(3)
        )
        data.judge_field_separation = _mean(f.separation for f in qualified if f.separation is not None)
    return data


def _load(session: Session) -> CategoryData:
    rows = session.exec(
        select(
            SpeakerScore.debater_id,
            SpeakerScore.position,
            SpeakerScore.content,
            SpeakerScore.style,
            SpeakerScore.strategy,
            SpeakerScore.ballot_id,
            SpeakerScore.side,
            Ballot.judge_id,
            Ballot.winner,
            Ballot.discarded,
            Ballot.forfeit,
        )
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .where(
            SpeakerScore.content.is_not(None),
            SpeakerScore.style.is_not(None),
            SpeakerScore.strategy.is_not(None),
        )
    ).all()
    return build_category_data(
        Mark(
            debater_id=debater_id, position=int(position), content=content, style=style, strategy=strategy,
            ballot_id=ballot_id, judge_id=judge_id, side=side.value, winner=winner.value,
            discarded=discarded, forfeit=forfeit,
        )
        for (debater_id, position, content, style, strategy, ballot_id, side, judge_id, winner, discarded,
             forfeit) in rows
    )


def category_data(session: Session) -> CategoryData:
    return cache.cached("categories", lambda: _load(session))


def judge_categories(data: CategoryData, judge_id: int) -> JudgeCategories | None:
    figures = data.judges.get(judge_id)
    if figures is None or figures.ballots < MIN_JUDGE_BALLOTS:
        return None
    avg_field = data.judge_field_average or (None, None, None)
    spread_field = data.judge_field_spread or (None, None, None)
    categories = [
        JudgeCategory(
            category=name,
            scale=SCALES[name],
            average=metric(figures.average[i], avg_field[i]),
            spread=metric(figures.spread[i], spread_field[i]),
            decides=metric(figures.decides[i], data.judge_field_decides[i], digits=1),
        )
        for i, name in enumerate(CATEGORIES)
    ]
    separation = (
        None
        if figures.separation is None
        else metric(figures.separation, data.judge_field_separation)
    )
    return JudgeCategories(ballots=figures.ballots, categories=categories, separation=separation)
