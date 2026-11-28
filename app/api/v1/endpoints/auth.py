from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlmodel import Field, SQLModel, select

from app.api.deps import CurrentUserDep, SessionDep
from app.core.config import settings
from app.core.security import burn_password_check, verify_password
from app.models import User, UserPublic
from app.models.user import MAX_PASSWORD_LENGTH, normalize_email
from app.services import login_throttle
from app.services.auth import create_auth_session, revoke_session

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(SQLModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


def _invalid_login() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
    )


@router.post("/login", response_model=UserPublic)
def login(
    login_in: LoginRequest, request: Request, response: Response, session: SessionDep
) -> User:
    email = normalize_email(login_in.email)
    ip = request.client.host if request.client else None
    if (wait := login_throttle.retry_after(ip, email)) is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed sign-in attempts. Try again later.",
            headers={"Retry-After": str(wait)},
        )
    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        burn_password_check(login_in.password)
        login_throttle.record_failure(ip, email)
        raise _invalid_login()
    if not verify_password(login_in.password, user.password_hash) or not user.is_active:
        login_throttle.record_failure(ip, email)
        raise _invalid_login()
    login_throttle.record_success(ip, email)

    token = create_auth_session(session, user)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )
    session.refresh(user)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, session: SessionDep) -> None:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        revoke_session(session, token)
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


@router.get("/me", response_model=UserPublic)
def me(user: CurrentUserDep) -> User:
    return user
