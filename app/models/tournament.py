from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel

from app.models.enums import DebateFormat

if TYPE_CHECKING:
    from app.models.round import Round
    from app.models.team import Team


class TournamentBase(SQLModel):
    name: str
    abbr: str | None = None
    base_url: str | None = None
    slug: str = Field(unique=True, index=True)
    date: date | None = None
    format: DebateFormat = DebateFormat.TWO_TEAM


class Tournament(TournamentBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    rounds: list["Round"] = Relationship(
        back_populates="tournament",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    teams: list["Team"] = Relationship(
        back_populates="tournament",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class TournamentCreate(TournamentBase):
    pass


class TournamentPublic(TournamentBase):
    id: int
    created_at: datetime


class TournamentUpdate(SQLModel):
    name: str | None = None
    abbr: str | None = None
    slug: str | None = None
    base_url: str | None = None
    date: date | None = None
    format: DebateFormat | None = None
