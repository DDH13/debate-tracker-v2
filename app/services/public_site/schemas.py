"""Response types shared by the public page endpoints."""

from datetime import date

from sqlmodel import SQLModel

from app.models import InstitutionKind, Side


class InstitutionRef(SQLModel):
    id: int
    name: str
    code: str | None = None
    kind: InstitutionKind | None = None


class PersonRef(SQLModel):
    id: int
    name: str


class TeamRef(SQLModel):
    id: int
    name: str
    institution: InstitutionRef | None = None


class TournamentRef(SQLModel):
    id: int
    name: str
    abbr: str | None = None
    slug: str
    date: date | None = None


class RoundRef(SQLModel):
    id: int
    seq: int
    name: str | None = None
    abbr: str | None = None
    is_elimination: bool = False


class Metric(SQLModel):
    """A figure next to the field it is measured against. `field_mean` is the mean over
    comparable people (see `field.py`); `delta` is `value - field_mean`."""

    value: float | None = None
    field_mean: float | None = None
    delta: float | None = None
    percentile: float | None = None


class WinLoss(SQLModel):
    wins: int = 0
    losses: int = 0
    rate: float | None = None  # percent


class SideSplit(SQLModel):
    prop: WinLoss = WinLoss()
    opp: WinLoss = WinLoss()


class DebateVotes(SQLModel):
    prop: int = 0
    opp: int = 0


class MotionExcerpt(SQLModel):
    text: str
    info_slide: str | None = None


class DebateRow(SQLModel):
    """One debate in a list: used by the explorer, and by tournament / judge pages."""

    id: int
    tournament: TournamentRef
    round: RoundRef
    prop_team: TeamRef
    opp_team: TeamRef
    winner: Side | None = None
    votes: DebateVotes = DebateVotes()
    margin: float | None = None  # panel-averaged prop team total minus opp team total (positive favours Proposition)
    motion: str | None = None
    chair: PersonRef | None = None


class Page(SQLModel):
    total: int
    offset: int
    limit: int


def metric(value: float | None, field_mean: float | None, percentile: float | None = None, digits: int = 2) -> Metric:
    return Metric(
        value=None if value is None else round(value, 4),
        field_mean=None if field_mean is None else round(field_mean, 4),
        delta=None if value is None or field_mean is None else round(value - field_mean, digits),
        percentile=None if percentile is None else round(percentile, 1),
    )


def win_loss(wins: int, losses: int) -> WinLoss:
    total = wins + losses
    return WinLoss(wins=wins, losses=losses, rate=round(wins / total * 100, 2) if total else None)
