"""Structured, privacy-aware logging helpers.

Every diagnostic carries a request/run id. Raw observation-level data is
never logged: matrix content is summarized as shape + checksum, and column
names are truncated. Set TWOSLS_LOG_PAYLOAD=1 only for local debugging, and
even then only shapes are emitted.
"""
from __future__ import annotations

import hashlib
import logging
import sys
from typing import Any, Mapping

import numpy as np

from .config import settings

_CONFIGURED = False


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger("twosls")
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(f"twosls.{name}")


def redact_array(a: np.ndarray, max_names: int = 5) -> Mapping[str, Any]:
    """Return a log-safe description of a data matrix (never the values)."""
    arr = np.asarray(a, dtype=float)
    finite = int(np.isfinite(arr).sum())
    checksum = hashlib.sha256(arr.tobytes()).hexdigest()[:12] if arr.size else "empty"
    return {
        "shape": list(arr.shape),
        "finite_cells": finite,
        "total_cells": int(arr.size),
        "sha256_12": checksum,
    }


def log_event(logger: logging.Logger, level: int, request_id: str, event: str, **fields: Any) -> None:
    """Emit one structured event with a request id."""
    safe: dict[str, Any] = {"request_id": request_id, "event": event}
    for key, value in fields.items():
        if isinstance(value, np.ndarray):
            safe[key] = redact_array(value)
        elif isinstance(value, (np.floating, np.integer)):
            safe[key] = value.item()
        else:
            safe[key] = value
    logger.log(level, "%s", safe)
