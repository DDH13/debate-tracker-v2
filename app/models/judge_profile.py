from datetime import datetime, timezone

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class JudgeProfileBase(SQLModel):
    prelims_judged: int = 0
    elims_judged: int = 0
    total_judged: int = 0
    tournaments_judged: int = 0
    activity_percentile: float | None = None

    average_first: float | None = None
    average_second: float | None = None
    average_third: float | None = None
    average_reply: float | None = None
    average_substantive: float | None = None
    score_stdev: float | None = None

    leniency_count: int = 0
    harshness_count: int = 0
    neutral_count: int = 0
    leniency: float | None = None
    harshness: float | None = None
    overall_sentiment: float | None = None
    speeches_considered: int = 0

    ballots_cast: int = 0
    dissents: int = 0
    dissent_rate: float | None = None


class JudgeProfile(JudgeProfileBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    judge_id: int = Field(foreign_key="judge.id", unique=True)
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    round_preferences: dict[str, int] = Field(default_factory=dict, sa_column=Column(JSON))


class JudgeProfilePublic(JudgeProfileBase):
    id: int
    judge_id: int
    computed_at: datetime
    round_preferences: dict[str, int]
