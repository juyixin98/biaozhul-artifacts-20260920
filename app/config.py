"""Application configuration loaded from environment variables.

No code changes are required between environments: the demo data and demo API
keys only come into existence when the ``bootstrap_demo`` routine runs
automatically on first container start (see ``app/bootstrap.py``).
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CV_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://consentvault:consentvault@localhost:5432/consentvault"

    # Optional fixed demo keys. When unset, random keys are generated and printed
    # to the logs during bootstrap. These are convenience credentials for local
    # demos only; never reuse them outside a throwaway environment.
    demo_admin_key: str | None = None
    demo_auditor_key: str | None = None
    demo_org2_admin_key: str | None = None
    demo_org2_auditor_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
