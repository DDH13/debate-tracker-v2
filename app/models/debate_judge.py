from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

if TYPE_CHECKING:
    from app.models.debate import Debate
    from app.models.judge import Judge


class DebateJudgeBase(SQLModel):
    is_chair: bool = False


class DebateJudge(DebateJudgeBase, table=True):
    __table_args__ = (UniqueConstraint("debate_id", "judge_id"),)

    id: int | None = Field(default=None, primary_key=True)
    debate_id: int = Field(foreign_key="debate.id")
    judge_id: int = Field(foreign_key="judge.id")

    debate: "Debate" = Relationship(back_populates="judge_links")
    judge: "Judge" = Relationship(back_populates="debate_links")


class DebateJudgeCreate(SQLModel):
    judge_id: int
    is_chair: bool = False


class DebateJudgePublic(DebateJudgeBase):
    id: int
    debate_id: int
    judge_id: int
