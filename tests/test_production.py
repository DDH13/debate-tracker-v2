"""Production hardening: config guards, the cross-site write guard, the login brake, health,
session purging and list bounds."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import Settings, settings
from app.models import AuthSession, UserRole
from tests.test_auth import _make_user

LOGIN = "/api/v1/auth/login"


# --- config ---------------------------------------------------------------------------


def _prod(**overrides) -> Settings:
    values = {
        "environment": "production",
        "database_url": "postgresql://u:p@db/dt",
        "cors_origins": ["https://stats.example.org"],
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_settings_accept_a_complete_config() -> None:
    prod = _prod(session_cookie_secure=False)
    assert prod.session_cookie_secure is True  # forced on
    assert prod.docs_enabled is False
    assert _prod(docs_enabled=True).docs_enabled is True


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"database_url": "sqlite://"}, "DATABASE_URL"),
        ({"cors_origins": ["*"]}, "CORS_ORIGINS"),
        ({"seed_on_startup": True}, "SEED_ON_STARTUP"),
    ],
)
def test_production_settings_refuse_unsafe_values(overrides: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _prod(**overrides)


def test_development_settings_keep_docs_on() -> None:
    assert Settings(_env_file=None).docs_enabled is True


# --- cross-site write guard ------------------------------------------------------------


@pytest.fixture
def explicit_origins(monkeypatch) -> None:
    monkeypatch.setattr(settings, "cors_origins", ["https://stats.example.org"])


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Referer": "https://evil.example/page"},
        {"Origin": "null"},
    ],
)
def test_cross_site_write_is_refused(
    client: TestClient, explicit_origins: None, headers: dict[str, str]
) -> None:
    resp = client.post("/api/v1/stats/refresh", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Cross-site request refused"


@pytest.mark.parametrize(
    "headers",
    [
        {},  # non-browser client
        {"Origin": "http://testserver"},  # the API's own host
        {"Origin": "https://stats.example.org"},  # the allowed UI origin
        {"Origin": "https://ui.example", "X-Forwarded-Host": "ui.example"},  # proxied
    ],
)
def test_same_site_write_is_allowed(
    client: TestClient, explicit_origins: None, headers: dict[str, str]
) -> None:
    resp = client.post("/api/v1/stats/refresh", headers=headers)
    assert resp.status_code != 403


def test_cross_site_read_is_not_blocked(client: TestClient, explicit_origins: None) -> None:
    resp = client.get("/api/v1/tournaments", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 200


def test_guard_is_off_with_wildcard_origins(client: TestClient) -> None:
    assert "*" in settings.cors_origins  # the development default
    resp = client.post("/api/v1/stats/refresh", headers={"Origin": "https://evil.example"})
    assert resp.status_code != 403


# --- login brake ----------------------------------------------------------------------


def test_repeated_failed_logins_are_throttled(
    anon_client: TestClient, session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "login_max_failures", 3)
    _make_user(session)
    for _ in range(3):
        resp = anon_client.post(LOGIN, json={"email": "chair@dc.lk", "password": "wrong-pass"})
        assert resp.status_code == 401
    blocked = anon_client.post(LOGIN, json={"email": "chair@dc.lk", "password": "correct-horse"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0


def test_successful_login_clears_failures(
    anon_client: TestClient, session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "login_max_failures", 3)
    _make_user(session)
    for _ in range(2):
        anon_client.post(LOGIN, json={"email": "chair@dc.lk", "password": "wrong-pass"})
    assert anon_client.post(
        LOGIN, json={"email": "chair@dc.lk", "password": "correct-horse"}
    ).status_code == 200
    for _ in range(2):
        resp = anon_client.post(LOGIN, json={"email": "chair@dc.lk", "password": "wrong-pass"})
        assert resp.status_code == 401  # the earlier two were forgotten


def test_unknown_emails_count_against_the_ip(
    anon_client: TestClient, session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "login_max_failures", 3)
    _make_user(session)
    for i in range(3):
        anon_client.post(LOGIN, json={"email": f"guess{i}@dc.lk", "password": "wrong-pass"})
    resp = anon_client.post(LOGIN, json={"email": "chair@dc.lk", "password": "correct-horse"})
    assert resp.status_code == 429


def test_oversized_password_is_rejected_before_hashing(anon_client: TestClient) -> None:
    resp = anon_client.post(LOGIN, json={"email": "chair@dc.lk", "password": "x" * 10_000})
    assert resp.status_code == 422


def test_user_password_has_a_maximum(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/admin/users",
        json={"email": "v@dc.lk", "name": "V", "role": "volunteer", "password": "x" * 129},
    )
    assert resp.status_code == 422


# --- sessions -------------------------------------------------------------------------


def test_login_purges_everyones_expired_sessions(
    anon_client: TestClient, session: Session
) -> None:
    other = _make_user(session, email="vol@dc.lk", role=UserRole.VOLUNTEER)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    session.add(AuthSession(token_hash="stale", user_id=other.id, expires_at=past))
    session.commit()
    _make_user(session)
    assert anon_client.post(
        LOGIN, json={"email": "chair@dc.lk", "password": "correct-horse"}
    ).status_code == 200
    hashes = session.exec(select(AuthSession.token_hash)).all()
    assert "stale" not in hashes
    assert len(hashes) == 1


# --- health and bounds ----------------------------------------------------------------


def test_health_checks_the_database(anon_client: TestClient) -> None:
    resp = anon_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/debaters?offset=-1",
        "/api/v1/tournaments?limit=0",
        "/api/v1/stats/debater-profiles?offset=-5",
        "/api/v1/admin/users?offset=-1",
    ],
)
def test_list_paging_is_bounded(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 422
