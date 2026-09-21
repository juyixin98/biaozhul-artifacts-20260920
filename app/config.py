from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, overridable through environment variables."""

    model_config = SettingsConfigDict(env_prefix="CAREFORCE_", env_file=".env", extra="ignore")

    # Peer-authenticated local socket by default; Docker Compose sets a TCP URL.
    database_url: str = "postgresql+psycopg2:///careforce?host=/var/run/postgresql"

    # Invitation must be accepted within this many minutes or it lapses.
    invitation_ttl_minutes: int = 8
    weekly_hour_cap: int = 44
    rest_between_shifts_hours: int = 10
    generation_horizon_days: int = 14

    require_coordinator: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
