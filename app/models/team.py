from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from app.models.institution import Institution

if TYPE_CHECKING:
    from app.models.debate import Debate
    from app.models.team_member import TeamMember
    from app.models.tournament import Tournament


class TeamBase(SQLModel):
    name: str


class Team(TeamBase, table=True):
    __table_args__ = (UniqueConstraint("tournament_id", "name"),)

    id: int | None = Field(default=None, primary_key=True)
    tournament_id: int = Field(foreign_key="tournament.id")
    institution_id: int | None = Field(default=None, foreign_key="institution.id")

    tournament: "Tournament" = Relationship(back_populates="teams")
    institution: Institution | None = Relationship(back_populates="teams")
    prop_debates: list["Debate"] = Relationship(
        back_populates="prop_team",
        sa_relationship_kwargs={"foreign_keys": "[Debate.prop_team_id]"},
    )
    opp_debates: list["Debate"] = Relationship(
        back_populates="opp_team",
        sa_relationship_kwargs={"foreign_keys": "[Debate.opp_team_id]"},
    )
    member_links: list["TeamMember"] = Relationship(
        back_populates="team",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class TeamCreate(TeamBase):
    institution_id: int | None = None


class TeamPublic(TeamBase):
    id: int
    tournament_id: int
    institution_id: int | None


class TeamUpdate(SQLModel):
    name: str | None = None
    institution_id: int | None = None
