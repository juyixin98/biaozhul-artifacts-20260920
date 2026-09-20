"""Application configuration.

All settings come from environment variables so the same image can run in
Docker, CI or locally.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://consent:consent@localhost:5432/consentvault"

    # Demo bootstrapping keys. The seed script creates exactly these keys so
    # that the demo / documentation can use stable credentials. Rotate in any
    # real deployment and leave these empty.
    seed_admin_key: str = "demo-admin-key"
    seed_auditor_key: str = "demo-auditor-key"


@lru_cache
def get_settings() -> Settings:
    return Settings()
