from typing import TYPE_CHECKING

from pydantic import field_validator
from sqlmodel import CheckConstraint, Field, Relationship, SQLModel, UniqueConstraint

from app.models.enums import Side, SpeakerPosition

if TYPE_CHECKING:
    from app.models.ballot import Ballot
    from app.models.debater import Debater


class SpeakerScoreBase(SQLModel):
    side: Side
    position: SpeakerPosition
    final_score: float

    @field_validator("final_score")
    @classmethod
    def final_score_must_be_half_point(cls, value: float) -> float:
        if (value * 2) % 1 != 0:
            raise ValueError("final_score must be a multiple of 0.5")
        return value


class SpeakerScore(SpeakerScoreBase, table=True):
    __table_args__ = (
        UniqueConstraint("ballot_id", "side", "position"),
        CheckConstraint("final_score >= 0 AND final_score <= 100"),
    )

    id: int | None = Field(default=None, primary_key=True)
    ballot_id: int = Field(foreign_key="ballot.id")
    debater_id: int = Field(foreign_key="debater.id")

    ballot: "Ballot" = Relationship(back_populates="scores")
    debater: "Debater" = Relationship(back_populates="scores")


class SpeakerScoreCreate(SpeakerScoreBase):
    debater_id: int


class SpeakerScorePublic(SpeakerScoreBase):
    id: int
    ballot_id: int
    debater_id: int
