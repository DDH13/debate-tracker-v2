from collections.abc import Iterator

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.core.config import settings

if settings.database_url.startswith("sqlite"):
    # `check_same_thread=False` + `StaticPool` is only safe here because
    # `sqlite://` is in-memory: every connection must share the same
    # underlying database, so there can only ever be one. For a real
    # (file or network) database this would force every request thread
    # onto a single shared connection instead of SQLAlchemy's normal
    # per-thread pool checkout.
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=settings.sql_echo,
    )

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_connection, _connection_record):
        # SQLite doesn't enforce foreign keys unless told to per-connection;
        # Postgres enforces them unconditionally, so this pragma is sqlite-only.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
else:
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        echo=settings.sql_echo,
    )


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
