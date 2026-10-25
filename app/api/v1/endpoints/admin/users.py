from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.api.deps import SessionDep, get_or_404
from app.core.config import settings
from app.core.security import hash_password
from app.models import PasswordReset, User, UserCreate, UserPublic, UserRole, UserUpdate
from app.services.auth import other_active_board_count, revoke_user_sessions

router = APIRouter(prefix="/users", tags=["admin: users"])


def _email_conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail="A user with this email already exists"
    )


@router.post("", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def create_user(user_in: UserCreate, session: SessionDep) -> User:
    user = User.model_validate(user_in, update={"password_hash": hash_password(user_in.password)})
    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise _email_conflict() from exc
    session.refresh(user)
    return user


@router.get("", response_model=list[UserPublic])
def list_users(
    session: SessionDep,
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[User]:
    return list(session.exec(select(User).order_by(User.name).offset(offset).limit(limit)).all())


@router.get("/{user_id}", response_model=UserPublic)
def get_user(user_id: int, session: SessionDep) -> User:
    return get_or_404(session, User, user_id)


@router.patch("/{user_id}", response_model=UserPublic)
def update_user(user_id: int, user_in: UserUpdate, session: SessionDep) -> User:
    user = get_or_404(session, User, user_id)
    changes = user_in.model_dump(exclude_unset=True)

    stays_board = changes.get("role", user.role) == UserRole.BOARD
    stays_active = changes.get("is_active", user.is_active)
    if (
        user.role == UserRole.BOARD
        and user.is_active
        and not (stays_board and stays_active)
        and other_active_board_count(session, user.id) == 0
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot demote or deactivate the last active board user",
        )

    user.sqlmodel_update(changes)
    if not user.is_active:
        revoke_user_sessions(session, user.id)
    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise _email_conflict() from exc
    session.refresh(user)
    return user


@router.post("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(
    user_id: int, password_in: PasswordReset, request: Request, session: SessionDep
) -> None:
    """Sets a new password and signs the user out everywhere, except the caller's own session
    when they reset their own password."""
    user = get_or_404(session, User, user_id)
    user.password_hash = hash_password(password_in.password)
    session.add(user)
    revoke_user_sessions(
        session, user.id, keep_token=request.cookies.get(settings.session_cookie_name)
    )
    session.commit()
