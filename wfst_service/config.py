"""Independent configuration layer.

Values come from environment variables with safe local defaults; no
secrets exist in this service (all data is local synthetic fixtures).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: str
    default_k: int
    max_k: int
    default_budget: int
    max_budget: int
    max_input_length: int
    host: str
    port: int
    log_level: str

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("WFST_DB_PATH", "data/wfst.sqlite3"),
            default_k=_env_int("WFST_DEFAULT_K", 5),
            max_k=_env_int("WFST_MAX_K", 100),
            default_budget=_env_int("WFST_DEFAULT_BUDGET", 100_000),
            max_budget=_env_int("WFST_MAX_BUDGET", 5_000_000),
            max_input_length=_env_int("WFST_MAX_INPUT_LENGTH", 128),
            host=os.environ.get("WFST_HOST", "127.0.0.1"),
            port=_env_int("WFST_PORT", 8000),
            log_level=os.environ.get("WFST_LOG_LEVEL", "INFO"),
        )


SETTINGS = Settings.from_env()
