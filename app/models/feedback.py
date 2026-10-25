from datetime import date, datetime, timezone

from sqlmodel import Field, SQLModel

from app.models.enums import FeedbackSentiment, FeedbackStatus


class FeedbackBase(SQLModel):
    received_on: date | None = None
    from_name: str | None = None
    channel: str | None = None
    content: str = Field(min_length=1)
    sentiment: FeedbackSentiment | None = None
    status: FeedbackStatus = FeedbackStatus.OPEN
    action_taken: str | None = None


class Feedback(FeedbackBase, table=True):
    """Feedback received by the DC. The subject FKs are all optional: a row can be about a
    school, a tournament, a judge, any combination, or nothing in particular."""

    id: int | None = Field(default=None, primary_key=True)
    institution_id: int | None = Field(default=None, foreign_key="institution.id", index=True)
    tournament_id: int | None = Field(
        default=None, foreign_key="tournament.id", index=True, ondelete="SET NULL"
    )
    judge_id: int | None = Field(
        default=None, foreign_key="judge.id", index=True, ondelete="SET NULL"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FeedbackCreate(FeedbackBase):
    institution_id: int | None = None
    tournament_id: int | None = None
    judge_id: int | None = None


class FeedbackPublic(FeedbackBase):
    id: int
    institution_id: int | None
    tournament_id: int | None
    judge_id: int | None
    created_at: datetime


class FeedbackUpdate(SQLModel):
    received_on: date | None = None
    from_name: str | None = None
    channel: str | None = None
    content: str | None = Field(default=None, min_length=1)
    sentiment: FeedbackSentiment | None = None
    status: FeedbackStatus | None = None
    action_taken: str | None = None
    institution_id: int | None = None
    tournament_id: int | None = None
    judge_id: int | None = None
