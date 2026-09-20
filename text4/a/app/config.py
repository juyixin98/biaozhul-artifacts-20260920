from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg://skillpulse:skillpulse@localhost:5432/skillpulse"
    )
    # Demo / test facility: allows overriding the system clock through the API.
    allow_clock_control: bool = True
    # How long a learner holds a seat before confirming, in hours.
    seat_hold_hours: int = 48
    seed_demo: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
