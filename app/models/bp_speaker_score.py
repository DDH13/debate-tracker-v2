from typing import TYPE_CHECKING

from pydantic import field_validator
from sqlmodel import CheckConstraint, Field, Relationship, SQLModel, UniqueConstraint

from app.models.enums import BPPosition, BPSide
from app.models.speaker_score import validate_half_point

if TYPE_CHECKING:
    from app.models.bp_ballot import BPBallot
    from app.models.debater import Debater


class BPSpeakerScoreBase(SQLModel):
    side: BPSide
    position: BPPosition
    final_score: float

    @field_validator("final_score")
    @classmethod
    def must_be_half_point(cls, value: float) -> float:
        return validate_half_point(value)


class BPSpeakerScore(BPSpeakerScoreBase, table=True):
    __table_args__ = (
        UniqueConstraint("bp_ballot_id", "side", "position"),
        CheckConstraint("final_score >= 0 AND final_score <= 100"),
    )

    id: int | None = Field(default=None, primary_key=True)
    bp_ballot_id: int = Field(foreign_key="bpballot.id")
    debater_id: int = Field(foreign_key="debater.id")

    ballot: "BPBallot" = Relationship(back_populates="scores")
    debater: "Debater" = Relationship(back_populates="bp_scores")


class BPSpeakerScoreCreate(BPSpeakerScoreBase):
    debater_id: int


class BPSpeakerScorePublic(BPSpeakerScoreBase):
    id: int
    bp_ballot_id: int
    debater_id: int
