"""Structured run logging.

Every request/factorization gets a ``run_id``. Log records contain the run
id, an input fingerprint, library versions and the current computation
step, so that a test or production log line can be correlated back to the
exact input and run that produced it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import platform
import sys
import uuid
from importlib.metadata import version
from pathlib import Path

from .config import settings


def library_versions() -> dict[str, str]:
    def safe_version(name: str) -> str:
        try:
            return version(name)
        except Exception:  # package missing in odd environments
            return "unknown"

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "numpy": safe_version("numpy"),
        "scipy": safe_version("scipy"),
        "mpmath": safe_version("mpmath"),
        "fastapi": safe_version("fastapi"),
    }


_RUN_LOGGER_NAME = "spd_factor.runs"
_configured = False


def _configure() -> logging.Logger:
    global _configured
    logger = logging.getLogger(_RUN_LOGGER_NAME)
    if not _configured:
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
        Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            Path(settings.log_dir) / "runs.log", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(file_handler)
        logger.propagate = False
        _configured = True
    return logger


def fingerprint(n: int, rows, cols, vals) -> str:
    """Deterministic SHA-256 over a COO triple (order independent)."""
    payload = json.dumps({
        "n": int(n),
        "rows": [int(x) for x in rows],
        "cols": [int(x) for x in cols],
        "vals": [float(x) for x in vals],
    }, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


class RunLogger:
    """Per-run logger emitting one JSON line per computation step."""

    def __init__(self, run_id: str | None = None,
                 fp: str | None = None) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.fingerprint = fp
        self._logger = _configure()
        self._step = 0

    def _emit(self, level: int, event: str, **fields) -> None:
        self._step += 1
        record = {
            "run_id": self.run_id,
            "step": self._step,
            "event": event,
            "fingerprint": self.fingerprint,
            **fields,
        }
        self._logger.log(level, json.dumps(record, sort_keys=True,
                                           default=str))

    def info(self, event: str, **fields) -> None:
        self._emit(logging.INFO, event, **fields)

    def warn(self, event: str, **fields) -> None:
        self._emit(logging.WARNING, event, **fields)

    def error(self, event: str, **fields) -> None:
        self._emit(logging.ERROR, event, **fields)

    def banner(self, n: int, nnz: int) -> None:
        self.info("run_start", dimension=n, nnz_input=nnz,
                  versions=library_versions())
