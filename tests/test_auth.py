import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.core.security import hash_password
from app.main import app
from app.models import AuthSession, User, UserRole

def _make_user(
    session: Session,
    email: str = "chair@dc.lk",
    password: str = "correct-horse",
    role: UserRole = UserRole.BOARD,
    is_active: bool = True,
) -> User:
    user = User(
        email=email,
        name="Chair",
        role=role,
        is_active=is_active,
        password_hash=hash_password(password),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _api_routes() -> list[tuple[str, str]]:
    # Read from the OpenAPI schema: `app.routes` nests included routers lazily, so walking it
    # directly misses most endpoints.
    routes = []
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            routes.append((method.upper(), re.sub(r"\{[^}]+\}", "1", path)))
    assert len(routes) > 50, "route enumeration found suspiciously few endpoints"
    return routes


# --- login / logout / me -------------------------------------------------------------


def test_login_sets_session_cookie_and_me_returns_user(
    anon_client: TestClient, session: Session
) -> None:
    _make_user(session)
    resp = anon_client.post(
        "/api/v1/auth/login", json={"email": "  Chair@DC.lk ", "password": "correct-horse"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "chair@dc.lk"
    assert body["role"] == "board"
    assert "password_hash" not in body

    set_cookie = resp.headers["set-cookie"]
    assert settings.session_cookie_name in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()

    me = anon_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "chair@dc.lk"


def test_session_token_is_stored_hashed(anon_client: TestClient, session: Session) -> None:
    _make_user(session)
    anon_client.post("/api/v1/auth/login", json={"email": "chair@dc.lk", "password": "correct-horse"})
    token = anon_client.cookies[settings.session_cookie_name]
    stored = session.exec(select(AuthSession)).one()
    assert stored.token_hash != token
    assert len(stored.token_hash) == 64


@pytest.mark.parametrize(
    ("email", "password"),
    [("chair@dc.lk", "wrong-password"), ("nobody@dc.lk", "correct-horse")],
)
def test_bad_credentials_are_a_generic_401(
    anon_client: TestClient, session: Session, email: str, password: str
) -> None:
    _make_user(session)
    resp = anon_client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid email or password"
    assert settings.session_cookie_name not in anon_client.cookies


def test_inactive_user_cannot_log_in(anon_client: TestClient, session: Session) -> None:
    _make_user(session, is_active=False)
    resp = anon_client.post(
        "/api/v1/auth/login", json={"email": "chair@dc.lk", "password": "correct-horse"}
    )
    assert resp.status_code == 401


def test_me_requires_session(anon_client: TestClient) -> None:
    assert anon_client.get("/api/v1/auth/me").status_code == 401


def test_logout_revokes_the_session(anon_client: TestClient, session: Session) -> None:
    _make_user(session)
    anon_client.post("/api/v1/auth/login", json={"email": "chair@dc.lk", "password": "correct-horse"})
    token = anon_client.cookies[settings.session_cookie_name]

    assert anon_client.post("/api/v1/auth/logout").status_code == 204
    assert session.exec(select(AuthSession)).all() == []

    # Replaying the old cookie must not work even if the browser kept it.
    anon_client.cookies.set(settings.session_cookie_name, token)
    assert anon_client.get("/api/v1/auth/me").status_code == 401


def test_logout_without_session_is_harmless(anon_client: TestClient) -> None:
    assert anon_client.post("/api/v1/auth/logout").status_code == 204


def test_expired_session_is_rejected_and_pruned(
    anon_client: TestClient, session: Session
) -> None:
    _make_user(session)
    anon_client.post("/api/v1/auth/login", json={"email": "chair@dc.lk", "password": "correct-horse"})
    stored = session.exec(select(AuthSession)).one()
    stored.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.add(stored)
    session.commit()

    assert anon_client.get("/api/v1/auth/me").status_code == 401
    assert session.exec(select(AuthSession)).all() == []


def test_deactivated_user_session_stops_working(client: TestClient, session: Session) -> None:
    user = _make_user(session, email="vol@dc.lk", role=UserRole.VOLUNTEER)
    with TestClient(app) as other:
        other.post("/api/v1/auth/login", json={"email": "vol@dc.lk", "password": "correct-horse"})
        assert other.get("/api/v1/auth/me").status_code == 200

        resp = client.patch(f"/api/v1/admin/users/{user.id}", json={"is_active": False})
        assert resp.status_code == 200
        assert other.get("/api/v1/auth/me").status_code == 401


# --- route protection ----------------------------------------------------------------


def _protected_routes() -> list[tuple[str, str]]:
    """Every write and every admin read, minus the auth endpoints themselves."""
    return [
        (method, path)
        for method, path in _api_routes()
        if not path.startswith("/api/v1/auth")
        and (method not in ("GET", "HEAD") or path.startswith("/api/v1/admin"))
    ]


@pytest.mark.parametrize(("method", "path"), _protected_routes())
def test_anonymous_cannot_write_or_reach_admin(
    anon_client: TestClient, method: str, path: str
) -> None:
    resp = anon_client.request(method, path)
    assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"


@pytest.mark.parametrize(("method", "path"), _protected_routes())
def test_volunteer_cannot_write_or_reach_admin(
    volunteer_client: TestClient, method: str, path: str
) -> None:
    resp = volunteer_client.request(method, path)
    assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"


def test_public_reads_stay_open(anon_client: TestClient) -> None:
    assert anon_client.get("/api/v1/tournaments").status_code == 200
    assert anon_client.get("/api/v1/institutions").status_code == 200


# --- admin user management -----------------------------------------------------------


def test_board_creates_user_who_can_then_log_in(client: TestClient, session: Session) -> None:
    resp = client.post(
        "/api/v1/admin/users",
        json={"email": "Vol@DC.lk", "name": "Vol", "role": "volunteer", "password": "s3cret-pass"},
    )
    assert resp.status_code == 201
    assert resp.json()["email"] == "vol@dc.lk"
    assert "password" not in resp.json()

    with TestClient(app) as other:
        login = other.post(
            "/api/v1/auth/login", json={"email": "vol@dc.lk", "password": "s3cret-pass"}
        )
        assert login.status_code == 200
        assert login.json()["role"] == "volunteer"


def test_create_user_validation_and_conflict(client: TestClient) -> None:
    short = client.post(
        "/api/v1/admin/users",
        json={"email": "a@dc.lk", "name": "A", "role": "board", "password": "short"},
    )
    assert short.status_code == 422

    payload = {"email": "a@dc.lk", "name": "A", "role": "board", "password": "long-enough"}
    assert client.post("/api/v1/admin/users", json=payload).status_code == 201
    dup = client.post("/api/v1/admin/users", json={**payload, "email": "A@dc.lk"})
    assert dup.status_code == 409


def test_list_and_get_users(client: TestClient) -> None:
    users = client.get("/api/v1/admin/users").json()
    assert [u["email"] for u in users] == ["board@test.local"]
    assert client.get(f"/api/v1/admin/users/{users[0]['id']}").status_code == 200
    assert client.get("/api/v1/admin/users/999").status_code == 404


def test_cannot_remove_last_active_board_user(client: TestClient) -> None:
    me = client.get("/api/v1/auth/me").json()
    demote = client.patch(f"/api/v1/admin/users/{me['id']}", json={"role": "volunteer"})
    assert demote.status_code == 409
    deactivate = client.patch(f"/api/v1/admin/users/{me['id']}", json={"is_active": False})
    assert deactivate.status_code == 409

    # With a second board user it's allowed.
    client.post(
        "/api/v1/admin/users",
        json={"email": "b@dc.lk", "name": "B", "role": "board", "password": "long-enough"},
    )
    rename = client.patch(f"/api/v1/admin/users/{me['id']}", json={"name": "Renamed"})
    assert rename.status_code == 200
    demote = client.patch(f"/api/v1/admin/users/{me['id']}", json={"role": "volunteer"})
    assert demote.status_code == 200
    # The demoted caller is no longer board, so admin access ends immediately.
    assert client.get("/api/v1/admin/users").status_code == 403


def test_reset_password_signs_target_out_but_keeps_callers_session(
    client: TestClient, session: Session
) -> None:
    user = _make_user(session, email="vol@dc.lk", role=UserRole.VOLUNTEER)
    with TestClient(app) as other:
        other.post("/api/v1/auth/login", json={"email": "vol@dc.lk", "password": "correct-horse"})

        resp = client.post(
            f"/api/v1/admin/users/{user.id}/password", json={"password": "brand-new-pass"}
        )
        assert resp.status_code == 204
        assert other.get("/api/v1/auth/me").status_code == 401

        old = other.post(
            "/api/v1/auth/login", json={"email": "vol@dc.lk", "password": "correct-horse"}
        )
        assert old.status_code == 401
        new = other.post(
            "/api/v1/auth/login", json={"email": "vol@dc.lk", "password": "brand-new-pass"}
        )
        assert new.status_code == 200

    me = client.get("/api/v1/auth/me").json()
    own = client.post(f"/api/v1/admin/users/{me['id']}/password", json={"password": "my-new-pass"})
    assert own.status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 200
