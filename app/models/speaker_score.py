from typing import TYPE_CHECKING

from pydantic import field_validator, model_validator
from sqlmodel import CheckConstraint, Field, Relationship, SQLModel, UniqueConstraint

from app.models.enums import Side, SpeakerPosition

if TYPE_CHECKING:
    from app.models.ballot import Ballot
    from app.models.debater import Debater


class SpeakerScoreBase(SQLModel):
    side: Side
    position: SpeakerPosition
    content: float | None = Field(default=None, ge=0, le=40)
    style: float | None = Field(default=None, ge=0, le=40)
    strategy: float | None = Field(default=None, ge=0, le=20)
    final_score: float

    @field_validator("content", "style", "strategy", "final_score")
    @classmethod
    def must_be_half_point(cls, value: float | None) -> float | None:
        if value is not None and (value * 2) % 1 != 0:
            raise ValueError("scores must be a multiple of 0.5")
        return value


class SpeakerScore(SpeakerScoreBase, table=True):
    __table_args__ = (
        UniqueConstraint("ballot_id", "side", "position"),
        CheckConstraint("final_score >= 0 AND final_score <= 100"),
        CheckConstraint("content IS NULL OR (content >= 0 AND content <= 40)"),
        CheckConstraint("style IS NULL OR (style >= 0 AND style <= 40)"),
        CheckConstraint("strategy IS NULL OR (strategy >= 0 AND strategy <= 20)"),
        CheckConstraint(
            "(content IS NULL AND style IS NULL AND strategy IS NULL)"
            " OR (content IS NOT NULL AND style IS NOT NULL AND strategy IS NOT NULL"
            "     AND final_score = content + style + strategy)"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    ballot_id: int = Field(foreign_key="ballot.id")
    debater_id: int = Field(foreign_key="debater.id")

    ballot: "Ballot" = Relationship(back_populates="scores")
    debater: "Debater" = Relationship(back_populates="scores")


class SpeakerScoreCreate(SpeakerScoreBase):
    debater_id: int
    final_score: float | None = None

    @model_validator(mode="after")
    def resolve_final_score(self) -> "SpeakerScoreCreate":
        parts = (self.content, self.style, self.strategy)
        given = [p for p in parts if p is not None]
        if given and len(given) != 3:
            raise ValueError("content, style and strategy must be provided together")
        if given:
            total = sum(given)
            if self.final_score is None:
                self.final_score = total
            elif self.final_score != total:
                raise ValueError(
                    f"final_score {self.final_score} must equal "
                    f"content + style + strategy ({total})"
                )
        elif self.final_score is None:
            raise ValueError("final_score is required when category scores are omitted")
        return self


class SpeakerScorePublic(SpeakerScoreBase):
    id: int
    ballot_id: int
    debater_id: int
