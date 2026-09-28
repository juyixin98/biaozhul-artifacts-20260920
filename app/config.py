"""Runtime configuration. Everything is local; no external accounts."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str
    log_level: str
    service_name: str
    service_version: str


def _default_db_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "var", "did_service.db")


def load_settings() -> Settings:
    return Settings(
        db_path=os.environ.get("DID_DB_PATH", _default_db_path()),
        log_level=os.environ.get("DID_LOG_LEVEL", "INFO").upper(),
        service_name="did-panel-service",
        service_version="1.0.0",
    )


SETTINGS = load_settings()
