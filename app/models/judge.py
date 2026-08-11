from typing import TYPE_CHECKING
from datetime import date
from sqlmodel import Field, Relationship, SQLModel

from app.models.institution import Institution
from app.models.participant import ParticipantBase

if TYPE_CHECKING:
    from app.models.ballot import Ballot
    from app.models.debate_judge import DebateJudge


class JudgeBase(ParticipantBase):
    pass


class Judge(JudgeBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    institution_id: int | None = Field(default=None, foreign_key="institution.id")

    institution: Institution | None = Relationship(back_populates="judges")
    debate_links: list["DebateJudge"] = Relationship(back_populates="judge")
    ballots: list["Ballot"] = Relationship(back_populates="judge")


class JudgeCreate(JudgeBase):
    institution_id: int | None = None


class JudgePublic(JudgeBase):
    id: int
    institution_id: int | None


class JudgeUpdate(SQLModel):
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    birthdate: date | None = None
    institution_id: int | None = None
    gender: str | None = None
