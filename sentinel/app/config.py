from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SENTINEL_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://sentinel:sentinel@localhost:5432/sentinel_b"
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720

    # Detection tuning
    workday_start_hour: int = 6
    workday_end_hour: int = 22
    download_window_minutes: int = 10
    download_threshold: int = 50
    baseline_days: int = 14
    baseline_zscore: float = 3.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
