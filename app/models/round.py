from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

if TYPE_CHECKING:
    from app.models.bp_debate import BPDebate
    from app.models.debate import Debate
    from app.models.motion import Motion
    from app.models.tournament import Tournament


class RoundBase(SQLModel):
    seq: int
    abbr: str | None = None
    name: str | None = None
    isElimination: bool = False
    completed: bool = False


class Round(RoundBase, table=True):
    __table_args__ = (UniqueConstraint("tournament_id", "seq"),)

    id: int | None = Field(default=None, primary_key=True)
    tournament_id: int = Field(foreign_key="tournament.id")

    tournament: "Tournament" = Relationship(back_populates="rounds")
    motion: "Motion" = Relationship(
        back_populates="round",
        sa_relationship_kwargs={"uselist": False, "cascade": "all, delete-orphan"},
    )
    debates: list["Debate"] = Relationship(
        back_populates="round",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    bp_debates: list["BPDebate"] = Relationship(
        back_populates="round",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class RoundCreate(RoundBase):
    pass


class RoundPublic(RoundBase):
    id: int
    tournament_id: int


class RoundUpdate(SQLModel):
    seq: int | None = None
    abbr: str | None = None
    name: str | None = None
    isElimination: bool | None = None
    completed: bool | None = None
