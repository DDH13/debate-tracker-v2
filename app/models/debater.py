from typing import TYPE_CHECKING
from datetime import date
from sqlmodel import Field, Relationship, SQLModel

from app.models.institution import Institution
from app.models.participant import ParticipantBase

if TYPE_CHECKING:
    from app.models.bp_speaker_score import BPSpeakerScore
    from app.models.speaker_score import SpeakerScore
    from app.models.team_member import TeamMember


class DebaterBase(ParticipantBase):
    pass


class Debater(DebaterBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    institution_id: int | None = Field(default=None, foreign_key="institution.id")

    institution: Institution | None = Relationship(back_populates="debaters")
    team_links: list["TeamMember"] = Relationship(
        back_populates="debater",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    scores: list["SpeakerScore"] = Relationship(back_populates="debater")
    bp_scores: list["BPSpeakerScore"] = Relationship(back_populates="debater")


class DebaterCreate(DebaterBase):
    institution_id: int | None = None


class DebaterPublic(DebaterBase):
    id: int
    institution_id: int | None


class DebaterUpdate(SQLModel):
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    birthdate: date | None = None
    gender: str | None = None
    institution_id: int | None = None
