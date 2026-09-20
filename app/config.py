from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # async driver for the application; alembic uses the sync equivalent
    database_url: str = "postgresql+asyncpg://workflow:workflow@localhost:5432/workflow"
    escalation_poll_interval: float = 2.0
    escalation_batch_limit: int = 20
    # the worker is disabled in unit tests
    enable_worker: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
