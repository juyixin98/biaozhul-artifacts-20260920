"""Runtime configuration, all sourced from environment variables with local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["Settings", "get_settings"]

ENGINE_VERSION = "miniowl-kernel/1.0.0"
SERVICE_NAME = "miniowl-restricted-class-expr"


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: str
    log_level: str
    log_file: str | None
    # Brute-force guard for the independent enumerator (synthetic fixtures only).
    max_oracle_classes: int

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("MINIOWL_DB", "data/miniowl.sqlite3"),
            log_level=os.environ.get("MINIOWL_LOG_LEVEL", "INFO"),
            log_file=os.environ.get("MINIOWL_LOG_FILE"),  # None -> stderr
            max_oracle_classes=int(os.environ.get("MINIOWL_ORACLE_MAX_CLASSES", "16")),
        )


def get_settings() -> Settings:
    return Settings.from_env()
