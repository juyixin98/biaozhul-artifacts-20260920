"""Application configuration loaded from environment variables.

Everything has a local, synthetic-data default so a first-time user can
start the server with zero configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("ATMS_DB_PATH", "data/atms.db")
    host: str = os.environ.get("ATMS_HOST", "127.0.0.1")
    port: int = _int_env("ATMS_PORT", 8000)
    # Default propagation budgets.
    max_label_envs: int = _int_env("ATMS_MAX_LABEL_ENVS", 64)
    max_total_envs: int = _int_env("ATMS_MAX_TOTAL_ENVS", 4096)
    max_steps: int = _int_env("ATMS_MAX_STEPS", 20000)
    log_redact: bool = os.environ.get("ATMS_LOG_REDACT", "1") != "0"


settings = Settings()
