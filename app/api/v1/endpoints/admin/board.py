from datetime import date

from fastapi import APIRouter, HTTPException, status
from sqlmodel import Session, col, or_

from app.api.crud import ListParams, add_crud_routes
from app.api.deps import SessionDep
from app.models import BoardRole, BoardRoleCreate, BoardRolePublic, BoardRoleUpdate, Person, User
from app.services import people as people_service

router = APIRouter()


class BoardRoleFilters(ListParams):
    custom = frozenset({"current"})

    person_id: int | None = None
    # Held today.
    current: bool | None = None


def _current_filter(statement, params: BoardRoleFilters):
    if params.current is None:
        return statement
    today = date.today()
    held = (BoardRole.term_start <= today) & or_(
        col(BoardRole.term_end).is_(None), col(BoardRole.term_end) >= today
    )
    return statement.where(held if params.current else ~held)


def _check_term(_session: Session, role: BoardRole, _user: User) -> None:
    if role.term_end is not None and role.term_end < role.term_start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="term_end must be on or after term_start",
        )


views = APIRouter(prefix="/board", tags=["admin: board"])


@views.get("", response_model=people_service.Board)
def get_board(session: SessionDep, on: date | None = None) -> people_service.Board:
    """Roles held on `on` (default today), roles starting later, and past terms."""
    return people_service.board(session, on)


router.include_router(views)

roles = APIRouter(prefix="/board-roles", tags=["admin: board"])
add_crud_routes(
    roles,
    noun="board_role",
    model=BoardRole,
    create_schema=BoardRoleCreate,
    public_schema=BoardRolePublic,
    update_schema=BoardRoleUpdate,
    filters=BoardRoleFilters,
    order_by=(col(BoardRole.term_start).desc(),),
    parents={"person_id": Person},
    on_save=_check_term,
    filter_hook=_current_filter,
)
router.include_router(roles)
