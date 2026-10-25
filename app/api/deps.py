from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, TypeVar

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session, SQLModel

from app.core.config import settings
from app.db.session import get_session
from app.models import User, UserRole
from app.services.auth import resolve_session
from app.services.merge import MergeConflict, MergeInvalid, MergeNotFound

SessionDep = Annotated[Session, Depends(get_session)]

ModelType = TypeVar("ModelType", bound=SQLModel)


def get_or_404(session: Session, model: type[ModelType], id: int) -> ModelType:
    obj = session.get(model, id)
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{model.__name__} {id} not found",
        )
    return obj


def get_current_user(request: Request, session: SessionDep) -> User:
    token = request.cookies.get(settings.session_cookie_name)
    user = resolve_session(session, token) if token else None
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


def require_board(user: CurrentUserDep) -> User:
    if user.role != UserRole.BOARD:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Board access required"
        )
    return user


BoardUserDep = Annotated[User, Depends(require_board)]


def require_board_for_writes(request: Request, session: SessionDep) -> None:
    """Router-level guard for the public resource routers: reads stay open, anything that
    changes data needs a board user."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    require_board(get_current_user(request, session))


@contextmanager
def merge_errors() -> Iterator[None]:
    """Translate the merge service's errors into 404 / 422 / 409 responses."""
    try:
        yield
    except MergeNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MergeInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except MergeConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
