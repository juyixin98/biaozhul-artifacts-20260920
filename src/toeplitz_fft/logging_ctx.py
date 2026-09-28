"""Run identity and structured logging helpers.

Every request / verification run receives a ``run_id`` that appears in every
log record and in the response, so test logs can be correlated with the
input that produced them.
"""

from __future__ import annotations

import logging
import platform
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import numpy as np
import scipy

from . import __version__
from .config import SETTINGS

_run_id_var: ContextVar[str] = ContextVar("run_id", default="-")


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def set_run_id(run_id: str) -> str:
    _run_id_var.set(run_id)
    return run_id


def get_run_id() -> str:
    return _run_id_var.get()


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id_var.get()
        return True


_CONFIGURED = False


def configure_logging(level: str | None = None) -> logging.Logger:
    """Idempotent logging configuration; emits run_id on each record."""
    global _CONFIGURED
    logger = logging.getLogger("toeplitz_fft")
    if _CONFIGURED:
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s [run=%(run_id)s] %(name)s: %(message)s"
        )
    )
    handler.addFilter(_RunIdFilter())
    logger.addHandler(handler)
    logger.setLevel(level or SETTINGS.log_level)
    logger.propagate = False
    _CONFIGURED = True
    return logger


def get_logger() -> logging.Logger:
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger("toeplitz_fft")


def environment_versions() -> dict[str, Any]:
    """Version block attached to evidence reports and service metadata."""
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "package": __version__,
    }
