"""Application configuration loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WF_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://wfuser:wfpass@127.0.0.1:5432/wfdb"
    # Interval between escalation sweeps in seconds.
    scheduler_interval_seconds: float = 1.0
    # Master switch for the in-process escalation scheduler.
    scheduler_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
