from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

if TYPE_CHECKING:
    from app.models.debater import Debater
    from app.models.team import Team


class TeamMemberBase(SQLModel):
    speaker_position: int | None = None


class TeamMember(TeamMemberBase, table=True):
    __table_args__ = (
        UniqueConstraint("team_id", "debater_id"),
        UniqueConstraint("team_id", "speaker_position"),
    )

    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id")
    debater_id: int = Field(foreign_key="debater.id")

    team: "Team" = Relationship(back_populates="member_links")
    debater: "Debater" = Relationship(back_populates="team_links")


class TeamMemberCreate(SQLModel):
    debater_id: int
    speaker_position: int | None = None


class TeamMemberPublic(TeamMemberBase):
    id: int
    team_id: int
    debater_id: int
