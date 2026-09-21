from __future__ import annotations

import base64
import hashlib
import logging

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("vaultcommand")

# Fixed, well-known key used ONLY when no master key is configured.
# Lets the app boot for local tests; production must set VAULT_MASTER_KEY_B64.
_DEV_MASTER_SECRET = b"vaultcommand-dev-master-key-do-not-use-in-prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAULT_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://vault:vault@localhost:5544/vault"
    # Base64-encoded 32-byte AES-GCM master key. When empty a deterministic dev
    # key is derived; booting with the dev key while environment=production fails.
    master_key_b64: str = ""
    environment: str = "development"
    # Per-wallet daily signing allowance, denominated in wei (UTC day).
    daily_quota_wei: int = 10**18
    # Minimum seconds between two successful signatures for one wallet.
    cooldown_seconds: int = 30
    # Dev-only: self-service API key registration endpoint.
    allow_registration: bool = True

    def master_key(self) -> bytes:
        if self.master_key_b64:
            try:
                key = base64.b64decode(self.master_key_b64, validate=True)
            except Exception:
                raise RuntimeError("VAULT_MASTER_KEY_B64 must be valid base64")
            if len(key) != 32:
                raise RuntimeError("VAULT_MASTER_KEY_B64 must decode to exactly 32 bytes")
            return key
        if self.environment.lower() == "production":
            raise RuntimeError(
                "Refusing to start: VAULT_MASTER_KEY_B64 is required in production"
            )
        logger.warning(
            "VAULT_MASTER_KEY_B64 not set - deriving a fixed DEVELOPMENT master key. "
            "Custodial keys are not secure outside local testing."
        )
        return hashlib.sha256(_DEV_MASTER_SECRET).digest()


settings = Settings()
