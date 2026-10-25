"""Tasks the board hands out. Only the board writes them; an assignee with a linked console
account can read their own (`my_tasks`)."""

from datetime import date, datetime, timezone

from sqlmodel import Session, SQLModel, col, select

from app.models import (
    Task,
    TaskPublic,
    TaskStatus,
    Tournament,
    Trip,
    User,
    Workshop,
)

OPEN_STATUSES = (TaskStatus.TODO, TaskStatus.IN_PROGRESS)


def apply_status_rules(task: Task, today: date | None = None) -> None:
    """A done task has a completion date (today unless one was given); any other status
    clears it, so reopening a task doesn't leave a stale date behind."""
    if task.status == TaskStatus.DONE:
        if task.completed_on is None:
            task.completed_on = today or date.today()
    else:
        task.completed_on = None


def stamp_task(task: Task, user: User) -> None:
    now = datetime.now(timezone.utc)
    if task.id is None:
        task.created_by_id = user.id
        task.created_at = now
    task.updated_at = now


def is_overdue(task: Task, today: date | None = None) -> bool:
    return task.status in OPEN_STATUSES and task.due_on is not None and task.due_on < (today or date.today())


class MyTask(TaskPublic):
    overdue: bool
    workshop_title: str | None = None
    trip_name: str | None = None
    tournament_name: str | None = None


class MyTasks(SQLModel):
    # False when the signed-in account isn't linked to a person, so nothing can be assigned
    # to it yet.
    linked: bool
    tasks: list[MyTask]


def _titles(session: Session, model, ids: set[int], attr: str) -> dict[int, str]:
    if not ids:
        return {}
    return {r.id: getattr(r, attr) for r in session.exec(select(model).where(col(model.id).in_(ids))).all()}


def my_tasks(session: Session, user: User) -> MyTasks:
    """The signed-in user's tasks (through their linked person): open ones first by due date
    (undated last), then finished ones, most recently completed first."""
    if user.person_id is None:
        return MyTasks(linked=False, tasks=[])
    tasks = session.exec(select(Task).where(Task.assignee_person_id == user.person_id)).all()
    workshops = _titles(session, Workshop, {t.workshop_id for t in tasks if t.workshop_id}, "title")
    trips = _titles(session, Trip, {t.trip_id for t in tasks if t.trip_id}, "name")
    tournaments = _titles(session, Tournament, {t.tournament_id for t in tasks if t.tournament_id}, "name")

    open_tasks = sorted(
        (t for t in tasks if t.status in OPEN_STATUSES),
        key=lambda t: (t.due_on is None, t.due_on or date.max, t.id),
    )
    closed = sorted(
        (t for t in tasks if t.status not in OPEN_STATUSES),
        key=lambda t: (t.completed_on or t.updated_at.date(), t.id),
        reverse=True,
    )
    return MyTasks(
        linked=True,
        tasks=[
            MyTask(
                **TaskPublic.model_validate(t).model_dump(),
                overdue=is_overdue(t),
                workshop_title=workshops.get(t.workshop_id),
                trip_name=trips.get(t.trip_id),
                tournament_name=tournaments.get(t.tournament_id),
            )
            for t in [*open_tasks, *closed]
        ],
    )
