from typing import TYPE_CHECKING

from sqlmodel import CheckConstraint, Field, Relationship, SQLModel

from app.models.enums import Side

if TYPE_CHECKING:
    from app.models.ballot import Ballot
    from app.models.debate_judge import DebateJudge
    from app.models.round import Round
    from app.models.team import Team


class DebateBase(SQLModel):
    room: str | None = None
    winner: Side | None = None


class Debate(DebateBase, table=True):
    __table_args__ = (CheckConstraint("prop_team_id != opp_team_id"),)

    id: int | None = Field(default=None, primary_key=True)
    round_id: int = Field(foreign_key="round.id")
    prop_team_id: int = Field(foreign_key="team.id")
    opp_team_id: int = Field(foreign_key="team.id")

    round: "Round" = Relationship(back_populates="debates")
    prop_team: "Team" = Relationship(
        back_populates="prop_debates",
        sa_relationship_kwargs={"foreign_keys": "[Debate.prop_team_id]"},
    )
    opp_team: "Team" = Relationship(
        back_populates="opp_debates",
        sa_relationship_kwargs={"foreign_keys": "[Debate.opp_team_id]"},
    )
    judge_links: list["DebateJudge"] = Relationship(
        back_populates="debate",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    ballots: list["Ballot"] = Relationship(
        back_populates="debate",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class DebateCreate(SQLModel):
    prop_team_id: int
    opp_team_id: int
    room: str | None = None
    winner: Side | None = None


class DebatePublic(DebateBase):
    id: int
    round_id: int
    prop_team_id: int
    opp_team_id: int


class DebateUpdate(SQLModel):
    room: str | None = None
    winner: Side | None = None
