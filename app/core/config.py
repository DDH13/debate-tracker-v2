from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Debate Tracker"
    version: str = "0.1.0"
    database_url: str = "sqlite://"
    sql_echo: bool = False
    # Run create_all and seed the default finance categories at startup. Tests turn it off so
    # TestClient never touches the .env database.
    init_db_on_startup: bool = True
    seed_on_startup: bool = False
    tabbycat_base_url: str | None = None
    tabbycat_api_key: str | None = None
    log_level: str = "DEBUG"
    import_trace: bool = False
    tabbycat_cache_dir: str | None = None
    # Console imports (app/services/imports.py): each checked tournament's upstream responses
    # are staged here so the import can replay them without the API key. Holds personal data
    # (speaker emails), so it is git-ignored and removed after import, discard or this many days.
    import_staging_dir: str = ".import-staging"
    import_staging_ttl_days: int = 7
    # An active job whose heartbeat is older than this was lost (server restart or crash).
    import_stale_seconds: int = 180
    # Run import jobs synchronously in the request instead of on the background thread (tests).
    import_run_inline: bool = False
    cors_origins: list[str] = ["*"]
    # In-process TTL cache behind the /public pages (tournament snapshots, field means).
    # 0 disables it. Each gunicorn worker has its own copy, so a write elsewhere is picked up
    # at the latest after this many seconds.
    public_cache_ttl_seconds: int = 120
    session_cookie_name: str = "dt_session"
    session_ttl_days: int = 14
    # True in production (HTTPS). A split UI/API deployment also needs samesite="none".
    session_cookie_secure: bool = False
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    @field_validator("database_url")
    @classmethod
    def _use_psycopg_driver(cls, value: str) -> str:
        # Hosts like Neon/Vercel hand out `postgres://` or `postgresql://` URLs, which
        # SQLAlchemy would route to psycopg2; the project depends on psycopg 3.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix):]
        return value


settings = Settings()
