"""Application configuration."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CAREFORCE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://careforce:careforce@localhost:5432/careforce"
    # Units every coordinator may schedule when they have no explicit grant.
    # Empty => coordinators may only schedule units they are explicitly granted.
    invitation_ttl_minutes: int = 8
    rest_between_shifts_hours: int = 10
    weekly_hour_cap: int = 44
    generation_horizon_days: int = 14
    # Background reaper tick in seconds. 0 disables the background loop (tests drive expiry manually).
    reaper_interval_seconds: float = 30.0
    enable_clock_api: bool = True
    api_key: str = ""  # optional static bearer token; empty disables auth


@lru_cache
def get_settings() -> Settings:
    return Settings()
