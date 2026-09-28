"""Application configuration (environment-driven, no secrets)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: str
    max_program_chars: int
    max_query_chars: int
    max_answers: int
    rate_limit_per_minute: int
    service_name: str

    @staticmethod
    def from_env() -> "Settings":
        base = Path(__file__).resolve().parents[1]
        default_db = str(base / "data" / "evidence.db")
        return Settings(
            db_path=os.environ.get("DATALOG_DB_PATH", default_db),
            max_program_chars=int(os.environ.get("DATALOG_MAX_PROGRAM_CHARS", "200000")),
            max_query_chars=int(os.environ.get("DATALOG_MAX_QUERY_CHARS", "2000")),
            max_answers=int(os.environ.get("DATALOG_MAX_ANSWERS", "1000")),
            # Per-client-IP sliding-window request cap; 0 disables limiting.
            rate_limit_per_minute=int(os.environ.get("DATALOG_RATE_LIMIT_PER_MIN", "120")),
            service_name="restricted-datalog",
        )


SETTINGS = Settings.from_env()
