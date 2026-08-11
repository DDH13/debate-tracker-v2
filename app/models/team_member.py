from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

if TYPE_CHECKING:
    from app.models.debater import Debater
    from app.models.team import Team


class TeamMember(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("team_id", "debater_id"),)

    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id")
    debater_id: int = Field(foreign_key="debater.id")

    team: "Team" = Relationship(back_populates="member_links")
    debater: "Debater" = Relationship(back_populates="team_links")


class TeamMemberCreate(SQLModel):
    debater_id: int


class TeamMemberPublic(SQLModel):
    id: int
    team_id: int
    debater_id: int
