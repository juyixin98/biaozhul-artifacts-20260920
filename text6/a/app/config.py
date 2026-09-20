from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONSENTVAULT_", env_file=".env", extra="ignore"
    )

    database_url: str = (
        "postgresql+psycopg2://consent:consent@localhost:5432/consentvault"
    )
    management_api_key: str = "change-me-management-key"
    # Read-only auditor key. Defaults to the management key in dev so a fresh
    # checkout works; production deployments must set a distinct value.
    auditor_api_key: str | None = None
    environment: str = "dev"

    @property
    def effective_auditor_key(self) -> str:
        return self.auditor_api_key or self.management_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
