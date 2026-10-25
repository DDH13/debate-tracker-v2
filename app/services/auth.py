from datetime import datetime, timedelta, timezone

from sqlmodel import Session, delete, func, select

from app.core.config import settings
from app.core.security import hash_token, new_session_token
from app.models import AuthSession, User, UserRole


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    # SQLite (and Postgres `timestamp without time zone`) hand datetimes back naive; they
    # were written as UTC, so reattach it before comparing against an aware now().
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def create_auth_session(session: Session, user: User) -> str:
    """Creates a session for `user`, prunes their expired ones, commits, and returns the raw
    cookie token (only its hash is stored)."""
    now = _utcnow()
    for stale in session.exec(select(AuthSession).where(AuthSession.user_id == user.id)).all():
        if _as_utc(stale.expires_at) <= now:
            session.delete(stale)
    token = new_session_token()
    session.add(
        AuthSession(
            token_hash=hash_token(token),
            user_id=user.id,
            created_at=now,
            expires_at=now + timedelta(days=settings.session_ttl_days),
        )
    )
    session.commit()
    return token


def resolve_session(session: Session, token: str) -> User | None:
    """The active user behind a cookie token, or None if it's unknown, expired, or belongs to
    a deactivated user."""
    auth_session = session.exec(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    ).first()
    if auth_session is None:
        return None
    if _as_utc(auth_session.expires_at) <= _utcnow():
        session.delete(auth_session)
        session.commit()
        return None
    user = session.get(User, auth_session.user_id)
    if user is None or not user.is_active:
        return None
    return user


def revoke_session(session: Session, token: str) -> None:
    session.exec(delete(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    session.commit()


def revoke_user_sessions(session: Session, user_id: int, keep_token: str | None = None) -> None:
    """Signs `user_id` out everywhere, optionally sparing the caller's own session. Does not
    commit."""
    statement = delete(AuthSession).where(AuthSession.user_id == user_id)
    if keep_token is not None:
        statement = statement.where(AuthSession.token_hash != hash_token(keep_token))
    session.exec(statement)


def other_active_board_count(session: Session, user_id: int) -> int:
    return session.exec(
        select(func.count())
        .select_from(User)
        .where(User.role == UserRole.BOARD, User.is_active, User.id != user_id)
    ).one()
