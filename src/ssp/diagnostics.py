"""Run identity and structured diagnostics.

Every planning run gets a ``run_id``.  Log records carry the run id together
with a compact fingerprint of the inputs so that log lines can be correlated
back to a specific request, and so that "interim peeks" (repeated analyses
under a fixed commitment) can be told apart from the single planned test.

The module also snapshots the numerical stack versions, which are written into
each run record for reproducibility.
"""
from __future__ import annotations

import hashlib
import json
import logging
import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy
import scipy

from .config import Settings, get_settings

_LOGGER_NAME = "ssp"


def numerical_versions() -> dict[str, str]:
    """Version snapshot of the numerical stack for the run record."""
    return {
        "python": platform.python_version(),
        "numpy": numpy.__version__,
        "scipy": scipy.__version__,
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
    }


def new_run_id() -> str:
    """Time-sortable run id: UTC timestamp + short random suffix."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"run-{ts}-{uuid.uuid4().hex[:8]}"


def input_fingerprint(payload: dict[str, Any]) -> str:
    """Stable short hash of the request inputs (for log correlation)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class RunLogger:
    """Structured logger bound to a run id.

    Emits both console records and, when configured, a per-run log file under
    ``settings.log_dir``.  Never swallows exceptions: failures propagate.
    """

    def __init__(self, run_id: str, fingerprint: str, settings: Settings | None = None):
        self.run_id = run_id
        self.fingerprint = fingerprint
        self.settings = settings or get_settings()
        self._logger = logging.getLogger(f"{_LOGGER_NAME}.{run_id}")
        self._logger.setLevel(self.settings.log_level)
        # Propagate to the root so host applications (uvicorn, pytest caplog)
        # own console handling; we additionally attach a per-run file handler.
        self._logger.propagate = True
        target_log = (self.settings.log_dir / f"{run_id}.log").resolve()
        if not any(
            isinstance(h, logging.FileHandler)
            and Path(getattr(h, "baseFilename", "")).resolve() == target_log
            for h in self._logger.handlers
        ):
            try:
                self.settings.log_dir.mkdir(parents=True, exist_ok=True)
                fh = logging.FileHandler(self.settings.log_dir / f"{run_id}.log", encoding="utf-8")
                fh.setFormatter(logging.Formatter(
                    "%(asctime)s | %(levelname)s | run=%(run_id)s fp=%(fingerprint)s | %(message)s"
                ))
                self._logger.addHandler(fh)
            except OSError:
                # File logging is best-effort; console/root logging still works.
                pass
        self._extra = {"run_id": run_id, "fingerprint": fingerprint}

    def _emit(self, level: int, event: str, **fields: Any) -> None:
        # Single-line JSON-ish payload keeps machine parsing straightforward.
        body = json.dumps({"event": event, **fields}, default=str, sort_keys=True)
        self._logger.log(level, body, extra=self._extra)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, **fields)

    def step(self, name: str, **fields: Any) -> None:
        """Record an explicit computation step with its inputs/outputs."""
        self._emit(logging.INFO, f"step:{name}", **fields)
