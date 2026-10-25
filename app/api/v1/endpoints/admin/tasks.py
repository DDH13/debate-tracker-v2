from datetime import date

from fastapi import APIRouter
from sqlmodel import Session, col

from app.api.crud import ListParams, add_crud_routes
from app.models import (
    Person,
    Task,
    TaskCreate,
    TaskPriority,
    TaskPublic,
    TaskStatus,
    TaskUpdate,
    Tournament,
    Trip,
    User,
    Workshop,
)
from app.services import tasks as task_service


class TaskFilters(ListParams):
    custom = frozenset({"open", "overdue"})

    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    assignee_person_id: int | None = None
    workshop_id: int | None = None
    trip_id: int | None = None
    tournament_id: int | None = None
    # To do or in progress.
    open: bool | None = None
    # Open and due before today.
    overdue: bool | None = None


def _task_filters(statement, params: TaskFilters):
    is_open = col(Task.status).in_(task_service.OPEN_STATUSES)
    if params.open is not None:
        statement = statement.where(is_open if params.open else ~is_open)
    if params.overdue:
        statement = statement.where(is_open, col(Task.due_on) < date.today())
    return statement


def _save_task(_session: Session, task: Task, user: User) -> None:
    task_service.apply_status_rules(task)
    task_service.stamp_task(task, user)


router = APIRouter(prefix="/tasks", tags=["admin: tasks"])
add_crud_routes(
    router,
    noun="task",
    model=Task,
    create_schema=TaskCreate,
    public_schema=TaskPublic,
    update_schema=TaskUpdate,
    filters=TaskFilters,
    # Dated tasks first, soonest first; undated after them.
    order_by=(col(Task.due_on).is_(None), col(Task.due_on), col(Task.created_at)),
    parents={"assignee_person_id": Person, "workshop_id": Workshop, "trip_id": Trip, "tournament_id": Tournament},
    on_save=_save_task,
    filter_hook=_task_filters,
)
