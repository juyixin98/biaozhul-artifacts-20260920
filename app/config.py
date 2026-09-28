"""Configuration loaded from environment variables (12-factor style)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str
    program_id: str
    log_level: str
    log_file: str | None
    service_name: str = "restricted-datalog"

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("DATALOG_DB_PATH", "data/evidence.db"),
            program_id=os.environ.get("DATALOG_PROGRAM_ID", "default"),
            log_level=os.environ.get("DATALOG_LOG_LEVEL", "INFO"),
            log_file=os.environ.get("DATALOG_LOG_FILE") or None,
        )
