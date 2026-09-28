"""Application configuration loaded from environment variables.

All values have local-safe defaults so the project runs from a clean checkout
without any production account.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "provenance.db"
DEFAULT_FIXTURE_GLOB = "*.sql"


@dataclass(frozen=True)
class Settings:
    db_path: str
    fixture_dir: str
    fixture_glob: str
    log_level: str
    # A query is rejected if its parsed plan exceeds this many algebra nodes.
    max_query_nodes: int
    # A single answer may carry at most this many witnesses (guard rail).
    max_witnesses_per_answer: int

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("PROVENANCE_DB_PATH", str(DEFAULT_DB_PATH)),
            fixture_dir=os.environ.get(
                "PROVENANCE_FIXTURE_DIR", str(BASE_DIR / "fixtures" / "data")
            ),
            fixture_glob=os.environ.get("PROVENANCE_FIXTURE_GLOB", DEFAULT_FIXTURE_GLOB),
            log_level=os.environ.get("PROVENANCE_LOG_LEVEL", "INFO").upper(),
            max_query_nodes=int(os.environ.get("PROVENANCE_MAX_QUERY_NODES", "200")),
            max_witnesses_per_answer=int(
                os.environ.get("PROVENANCE_MAX_WITNESSES_PER_ANSWER", "10000")
            ),
        )


settings = Settings.from_env()
