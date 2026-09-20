from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLOUDGATE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://cloudgate:cloudgate@127.0.0.1:5432/cloudgate_dev"
    # Secret used to sign device and session tokens. Override in every real deployment.
    token_secret: str = "cloudgate-dev-secret-change-me"
    # Bootstrap super-admin key; the super admin provisions tenants.
    super_admin_key: str = "super-admin-key"

    heartbeat_ttl_seconds: int = 600
    heartbeat_interval_seconds: int = 60
    sweeper_interval_seconds: float = 15.0
    run_sweeper: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
