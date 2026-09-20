"""Application settings, overridable via CLOUDGATE_* environment variables."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLOUDGATE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://cloudgate:cloudgate@localhost:5432/cloudgate"
    jwt_secret: str = "dev-secret-change-me"
    jwt_expiry_seconds: int = 3600 * 12

    # Device is expected to heartbeat every heartbeat_interval_seconds; a lease
    # with no heartbeat for lease_ttl_seconds is released by the sweeper.
    heartbeat_interval_seconds: int = 60
    lease_ttl_seconds: int = 600
    sweep_interval_seconds: int = 30  # <= 0 disables the background sweeper

    # Bootstrap global admin (created on startup if no admin with this name exists).
    admin_username: str = "admin"
    admin_password: str = "admin123"


def get_settings() -> Settings:
    return Settings()
