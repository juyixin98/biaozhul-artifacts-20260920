"""Logging configuration. Logs complement the SQLite evidence store:
the database holds structured, replayable run evidence; the log file holds
human-readable operational traces."""

from __future__ import annotations

import logging
import os
from pathlib import Path

_CONFIGURED = False


def configure_logging() -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("strips_planner")
    if _CONFIGURED:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_path = Path(os.environ.get("PLANNER_LOG", "logs/planner.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.WARNING)
    logger.addHandler(stream_handler)

    _CONFIGURED = True
    return logger
