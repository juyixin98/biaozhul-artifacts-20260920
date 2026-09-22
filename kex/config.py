"""Runtime configuration.

Everything is local-only. The database path is configurable through the
``KEX_DB_PATH`` environment variable so the Docker image can place SQLite on
the mounted volume.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    db_path: str
    echo_sql: bool
    # Maximum number of workers (across the whole local cluster) that may be
    # actively claiming job items at the same time. Hard cap per spec.
    max_workers: int
    # Run an embedded background worker thread inside the web process.
    embedded_worker: bool
    worker_poll_interval: float
    worker_lease_seconds: int
    worker_heartbeat_seconds: int

    @property
    def sqlalchemy_url(self) -> str:
        return f"sqlite:///{self.db_path}"


def load_config() -> Config:
    db_path = os.environ.get("KEX_DB_PATH", os.path.join(os.getcwd(), "data", "kex.db"))
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    return Config(
        db_path=db_path,
        echo_sql=os.environ.get("KEX_ECHO_SQL", "") == "1",
        max_workers=int(os.environ.get("KEX_MAX_WORKERS", "2")),
        embedded_worker=os.environ.get("KEX_EMBED_WORKER", "") == "1",
        worker_poll_interval=float(os.environ.get("KEX_WORKER_POLL_INTERVAL", "0.5")),
        worker_lease_seconds=int(os.environ.get("KEX_WORKER_LEASE_SECONDS", "60")),
        worker_heartbeat_seconds=int(os.environ.get("KEX_WORKER_HEARTBEAT_SECONDS", "10")),
    )
