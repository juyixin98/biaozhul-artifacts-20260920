"""Structured run journal.

Every operation emits one JSON line that can be correlated back to the caller
via ``run_id`` (and optional ``batch_id``). Records carry component versions,
the calculation stages that ran, the norms/counters that drove the decision and
an explicit ``verdict``. Exceptions and unknown states are logged as their own
verdict (``rejected`` / ``error`` / ``empty``) - they are never rewritten as
success.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import threading
import time
import uuid
from typing import Any

import numpy as np

try:  # FastAPI is a runtime dependency of the service, not of the numerical core
    import fastapi
except Exception:  # pragma: no cover
    fastapi = None  # type: ignore[assignment]

from . import __version__

_LOG_LOCK = threading.Lock()


def new_run_id() -> str:
    return f"run-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


def component_versions() -> dict[str, Any]:
    return {
        "service": __version__,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "fastapi": getattr(fastapi, "__version__", None) if fastapi else None,
        "platform": platform.platform(),
        "implementation": platform.python_implementation(),
    }


class RunJournal:
    """Thread-safe JSON-lines journal plus a stderr logger."""

    def __init__(self, log_path: str | None = None) -> None:
        self.log_path = log_path
        self._logger = logging.getLogger("sparse_embedding")
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)
        if log_path:
            os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)

    def record(
        self,
        *,
        run_id: str,
        event: str,
        verdict: str,
        stages: list[str] | None = None,
        batch_id: str | None = None,
        details: dict[str, Any] | None = None,
        error_code: str | None = None,
        elapsed_ms: float | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "run_id": run_id,
            "batch_id": batch_id,
            "event": event,
            "verdict": verdict,
            "stages": stages or [],
            "versions": component_versions(),
            "error_code": error_code,
            "elapsed_ms": elapsed_ms,
            "details": details or {},
        }
        line = json.dumps(entry, sort_keys=True, default=_json_default)
        with _LOG_LOCK:
            self._logger.info(line)
            if self.log_path:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        return entry


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)
