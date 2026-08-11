from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class DebaterProfileBase(SQLModel):
    tournaments_debated: int = 0
    prelims_debated: int = 0
    elims_debated: int = 0
    total_rounds: int = 0
    activity_percentile: float | None = None

    prelim_wins: int = 0
    prelim_losses: int = 0
    win_rate_prelims: float | None = None
    win_rate_prelims_percentile: float | None = None
    elim_wins: int = 0
    elim_losses: int = 0
    win_rate_elims: float | None = None
    win_rate_elims_percentile: float | None = None

    average_speaker_score: float | None = None
    speaker_score_stdev: float | None = None
    total_speeches: int = 0
    speaker_rank: int | None = None
    speaker_score_percentile: float | None = None
    iron_person_count: int = 0

    average_content: float | None = None
    average_style: float | None = None
    average_strategy: float | None = None


class DebaterProfile(DebaterProfileBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    debater_id: int = Field(foreign_key="debater.id", unique=True)
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    furthest_rounds: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    speaker_performances: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))


class DebaterProfilePublic(DebaterProfileBase):
    id: int
    debater_id: int
    computed_at: datetime
    furthest_rounds: list[dict[str, Any]]
    speaker_performances: list[dict[str, Any]]
