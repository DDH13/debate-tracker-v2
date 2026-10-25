from fastapi import APIRouter

from app.api.deps import CurrentUserDep, SessionDep
from app.services import tasks as task_service

# The only console data a volunteer can reach. Mounted outside `/admin` (which is
# board-only) and read-only by design: volunteers can't change their tasks.
router = APIRouter(prefix="/me", tags=["me"])


@router.get("/tasks", response_model=task_service.MyTasks)
def my_tasks(user: CurrentUserDep, session: SessionDep) -> task_service.MyTasks:
    """Tasks assigned to the signed-in user's linked person, open first."""
    return task_service.my_tasks(session, user)
