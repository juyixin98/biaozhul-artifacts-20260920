"""Runtime configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: str
    signing_key_path: str
    checkpoint_interval_seconds: float
    checkpoint_on_append: bool

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            data_dir=os.getenv("AUDIT_DATA_DIR", "./data"),
            signing_key_path=os.getenv("AUDIT_SIGNING_KEY", "./keys/audit_signing_key.pem"),
            checkpoint_interval_seconds=float(
                os.getenv("AUDIT_CHECKPOINT_INTERVAL", "5")
            ),
            checkpoint_on_append=os.getenv(
                "AUDIT_CHECKPOINT_ON_APPEND", "false"
            ).lower() in ("1", "true", "yes"),
        )


settings = Settings.from_env()


def ensure_signing_key(path: str | Path):
    """Load the server signing key, generating one on first run.

    The public half is written next to the key ONLY as a convenience for
    local demos. In a real deployment the trust anchor is distributed to
    verifiers out-of-band and must never be fetched from the audited server.
    """
    from . import keys as keymod

    p = Path(path)
    if p.exists():
        return keymod.load_private_key(p)
    key = keymod.generate_private_key()
    keymod.save_private_key(p, key)
    pub_pem = p.with_name(p.stem + ".public.pem")
    pub_pem.write_bytes(keymod.export_public_key_pem(key))
    return key
