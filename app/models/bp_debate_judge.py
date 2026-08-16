from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

if TYPE_CHECKING:
    from app.models.bp_debate import BPDebate
    from app.models.judge import Judge


class BPDebateJudgeBase(SQLModel):
    is_chair: bool = False
    is_trainee: bool = False


class BPDebateJudge(BPDebateJudgeBase, table=True):
    __table_args__ = (UniqueConstraint("bp_debate_id", "judge_id"),)

    id: int | None = Field(default=None, primary_key=True)
    bp_debate_id: int = Field(foreign_key="bpdebate.id")
    judge_id: int = Field(foreign_key="judge.id")

    bp_debate: "BPDebate" = Relationship(back_populates="judge_links")
    judge: "Judge" = Relationship(back_populates="bp_debate_links")


class BPDebateJudgeCreate(SQLModel):
    judge_id: int
    is_chair: bool = False
    is_trainee: bool = False


class BPDebateJudgePublic(BPDebateJudgeBase):
    id: int
    bp_debate_id: int
    judge_id: int
