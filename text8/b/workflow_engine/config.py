from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://workflow:workflow@localhost:5432/workflow"
    enable_sweeper: bool = False
    sweep_interval_seconds: float = 5.0
    seed_demo_on_start: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
