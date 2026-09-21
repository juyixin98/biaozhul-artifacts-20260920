from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, sourced from VAULT_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="VAULT_", env_file=".env", extra="ignore")

    # e.g. postgresql+psycopg2://vault:vault@db:5432/vault  (sqlite allowed for dev/tests)
    database_url: str = "sqlite:///./vault.db"
    # base64-encoded 32-byte AES-GCM master key. Required in real deployments.
    master_key: str = ""
    # Per-wallet daily signing quota denominated in wei (value + gas_limit*gas_price).
    daily_limit_wei: int = 10_000_000_000_000_000_000  # 10 ETH
    # Minimum seconds between two successful signatures on the same wallet.
    cooldown_seconds: float = 5.0


settings = Settings()
