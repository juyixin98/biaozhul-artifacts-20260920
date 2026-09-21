from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg2://skillpulse:skillpulse@localhost:5432/skillpulse"
    )
    # When false the background seat-expiry sweeper is disabled.
    # Tests set this to false and drive expiry through the maintenance API
    # so the clock is fully controllable.
    enable_sweeper: bool = True
    sweeper_interval_seconds: float = 30.0
    # How long a learner has to confirm an offered seat.
    seat_confirm_hours: int = 48


@lru_cache
def get_settings() -> Settings:
    return Settings()
