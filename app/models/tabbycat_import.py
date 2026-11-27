"""Tabbycat imports run from the console: a batch of tournaments, each checked (a dry run)
before a board member settles doubtful matches and imports it. See app/services/imports.py."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ImportBatch(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    created_by_id: int | None = Field(default=None, foreign_key="user.id", ondelete="SET NULL")
    created_at: datetime = Field(default_factory=_now)
    include_ballots: bool = True
    refresh_stats: bool = True
    stats_refreshed_at: datetime | None = None


class ImportJob(SQLModel, table=True):
    """One tournament in a batch. The API key is never stored: the check saves every upstream
    response to a staging directory, and the import replays from it."""

    id: int | None = Field(default=None, primary_key=True)
    batch_id: int = Field(foreign_key="importbatch.id", index=True, ondelete="CASCADE")
    position: int = 0
    base_url: str
    slug: str
    # queued_check | checking | ready | queued_import | importing | imported | failed | discarded
    # (plain text rather than a native enum, so adding a state needs no ALTER TYPE)
    status: str = "queued_check"
    phase: str | None = None
    progress_done: int = 0
    progress_total: int = 0
    error: str | None = None
    report: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    decisions: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    tournament_id: int | None = Field(
        default=None, foreign_key="tournament.id", index=True, ondelete="SET NULL"
    )
    checked_at: datetime | None = None
    finished_at: datetime | None = None
    # Touched by the process running the job; a stale active job was lost to a restart.
    updated_at: datetime = Field(default_factory=_now)
