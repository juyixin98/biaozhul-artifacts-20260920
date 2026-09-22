"""Embedded worker thread helper (single-container deployments)."""
from __future__ import annotations

from flask import Flask

from .config import Config
from .services.jobs import Worker


def start_embedded_worker(cfg: Config, app: Flask) -> Worker:
    worker = Worker(
        poll_interval=cfg.worker_poll_interval,
        lease_seconds=cfg.worker_lease_seconds,
        heartbeat_seconds=cfg.worker_heartbeat_seconds,
        max_workers=cfg.max_workers,
    )
    worker.start_in_thread()
    app.extensions["embedded_worker"] = worker
    return worker
