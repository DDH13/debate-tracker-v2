from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from app.models.enums import Side
from app.models.speaker_score import SpeakerScoreCreate, SpeakerScorePublic

if TYPE_CHECKING:
    from app.models.debate import Debate
    from app.models.judge import Judge
    from app.models.speaker_score import SpeakerScore


class BallotBase(SQLModel):
    winner: Side


class Ballot(BallotBase, table=True):
    __table_args__ = (UniqueConstraint("debate_id", "judge_id"),)

    id: int | None = Field(default=None, primary_key=True)
    debate_id: int = Field(foreign_key="debate.id")
    judge_id: int = Field(foreign_key="judge.id")

    debate: "Debate" = Relationship(back_populates="ballots")
    judge: "Judge" = Relationship(back_populates="ballots")
    scores: list["SpeakerScore"] = Relationship(
        back_populates="ballot",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class BallotCreate(SQLModel):
    judge_id: int
    winner: Side
    scores: list[SpeakerScoreCreate] | None = None


class BallotPublic(BallotBase):
    id: int
    debate_id: int
    judge_id: int
    discarded: bool = False
    forfeit: bool = False


class BallotPublicWithScores(BallotPublic):
    scores: list[SpeakerScorePublic]


class BallotUpdate(SQLModel):
    winner: Side | None = None
