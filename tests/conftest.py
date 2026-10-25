from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.deps import get_session
from app.core.config import settings
from app.main import app
from app.models import User, UserRole
from app.services.auth import create_auth_session

settings.init_db_on_startup = False  # never create tables or seed rows in the .env database
settings.seed_on_startup = False
settings.tabbycat_cache_dir = None  # tests must always hit the respx-mocked client fresh


@pytest.fixture(name="session")
def session_fixture() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_connection, _connection_record):
        # Match app/db/session.py (and Postgres): enforce foreign keys, so FK-ordering bugs
        # in deletes and merges fail here instead of only in production.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def sign_in(client: TestClient, session: Session, role: UserRole) -> User:
    """Creates a user with `role` (no usable password) and puts a live session cookie on
    `client`."""
    user = User(
        email=f"{role.value}@test.local", name=f"Test {role.value}", role=role, password_hash="!"
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    client.cookies.set(settings.session_cookie_name, create_auth_session(session, user))
    return user


@contextmanager
def _test_client(session: Session, role: UserRole | None) -> Iterator[TestClient]:
    def get_session_override() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_session] = get_session_override
    with TestClient(app) as client:
        if role is not None:
            sign_in(client, session, role)
        yield client
    app.dependency_overrides.clear()


@pytest.fixture(name="client")
def client_fixture(session: Session) -> Iterator[TestClient]:
    """Signed in as a board user, so every write endpoint is reachable."""
    with _test_client(session, UserRole.BOARD) as client:
        yield client


@pytest.fixture(name="anon_client")
def anon_client_fixture(session: Session) -> Iterator[TestClient]:
    with _test_client(session, None) as client:
        yield client


@pytest.fixture(name="volunteer_client")
def volunteer_client_fixture(session: Session) -> Iterator[TestClient]:
    with _test_client(session, UserRole.VOLUNTEER) as client:
        yield client
