"""Application configuration with explicit, overridable settings.

No secrets are required: everything is local.  Values are read from environment
variables at startup so the same artifact runs in tests (ephemeral SQLite) and
behind the API (file-backed SQLite) without code changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "planner.db"
DEFAULT_FIXTURE_DIR = REPO_ROOT / "fixtures"


@dataclass(frozen=True)
class Settings:
    db_path: str
    fixture_dir: str
    domain_dir: str
    log_level: str
    service_name: str
    service_version: str

    @classmethod
    def from_env(cls) -> "Settings":
        fixture_dir = Path(os.environ.get("HTN_FIXTURE_DIR", DEFAULT_FIXTURE_DIR))
        return cls(
            db_path=os.environ.get("HTN_DB_PATH", str(DEFAULT_DB_PATH)),
            fixture_dir=str(fixture_dir),
            domain_dir=str(fixture_dir / "domains"),
            log_level=os.environ.get("HTN_LOG_LEVEL", "INFO").upper(),
            service_name="finite-htn-planner",
            service_version="1.0.0",
        )


SETTINGS = Settings.from_env()
