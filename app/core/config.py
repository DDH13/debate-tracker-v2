from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Debate Tracker"
    version: str = "0.1.0"
    database_url: str = "sqlite://"
    sql_echo: bool = False
    seed_on_startup: bool = False
    tabbycat_base_url: str | None = None
    tabbycat_api_key: str | None = None
    log_level: str = "DEBUG"
    import_trace: bool = False
    tabbycat_cache_dir: str | None = None


settings = Settings()
