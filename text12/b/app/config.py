from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = "postgresql+psycopg2://cloudgate:cloudgate@localhost:5432/cloudgate"
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720

    platform_admin_username: str = "admin"
    platform_admin_password: str = "admin12345"

    # Lease lifecycle. Devices are expected to heartbeat every 60s; a lease
    # whose last heartbeat is older than the timeout is released by the sweeper.
    heartbeat_interval_seconds: int = 60
    heartbeat_timeout_seconds: int = 600
    sweep_interval_seconds: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
