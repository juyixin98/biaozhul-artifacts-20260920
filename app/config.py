"""Service configuration.

Everything is local by default: a file-backed SQLite database and a local log
file. No cloud accounts or external services are involved. Override paths via
environment variables when embedding the service elsewhere.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = BASE_DIR / "data"
DEFAULT_LOG_DIR = BASE_DIR / "logs"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_path: Path
    model_cap: int
    log_level: str
    service_name: str = "owl-restricted-service"

    @staticmethod
    def from_env() -> "Settings":
        data_dir = Path(os.environ.get("OWL_DATA_DIR", DEFAULT_DATA_DIR))
        log_dir = Path(os.environ.get("OWL_LOG_DIR", DEFAULT_LOG_DIR))
        data_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        return Settings(
            db_path=Path(os.environ.get("OWL_DB_PATH", data_dir / "evidence.db")),
            log_path=Path(os.environ.get("OWL_LOG_PATH", log_dir / "service.log")),
            model_cap=int(os.environ.get("OWL_MODEL_CAP", "100000")),
            log_level=os.environ.get("OWL_LOG_LEVEL", "INFO"),
        )
