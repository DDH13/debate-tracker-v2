from datetime import date, datetime, timezone

from sqlmodel import Field, SQLModel

from app.models.enums import TaskPriority, TaskStatus


class TaskBase(SQLModel):
    title: str = Field(min_length=1)
    description: str | None = None
    due_on: date | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.TODO
    # Stamped when the task is marked done (unless given), cleared if it's reopened.
    completed_on: date | None = None
    completion_note: str | None = None


class Task(TaskBase, table=True):
    """Work the board hands to someone in People. Only the board creates or changes tasks;
    the assignee sees their own under My tasks (read-only) through their linked account."""

    id: int | None = Field(default=None, primary_key=True)
    assignee_person_id: int | None = Field(
        default=None, foreign_key="person.id", index=True, ondelete="SET NULL"
    )
    workshop_id: int | None = Field(default=None, foreign_key="workshop.id", index=True, ondelete="SET NULL")
    trip_id: int | None = Field(default=None, foreign_key="trip.id", index=True, ondelete="SET NULL")
    tournament_id: int | None = Field(
        default=None, foreign_key="tournament.id", index=True, ondelete="SET NULL"
    )
    created_by_id: int | None = Field(default=None, foreign_key="user.id", ondelete="SET NULL")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TaskCreate(TaskBase):
    assignee_person_id: int | None = None
    workshop_id: int | None = None
    trip_id: int | None = None
    tournament_id: int | None = None


class TaskPublic(TaskBase):
    id: int
    assignee_person_id: int | None
    workshop_id: int | None
    trip_id: int | None
    tournament_id: int | None
    created_by_id: int | None
    created_at: datetime
    updated_at: datetime


class TaskUpdate(SQLModel):
    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    due_on: date | None = None
    priority: TaskPriority | None = None
    status: TaskStatus | None = None
    completed_on: date | None = None
    completion_note: str | None = None
    assignee_person_id: int | None = None
    workshop_id: int | None = None
    trip_id: int | None = None
    tournament_id: int | None = None
