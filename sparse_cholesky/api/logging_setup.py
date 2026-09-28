"""Run-correlated JSONL diagnostics.

Every request gets a ``run_id`` that appears in every log record it emits, so
a log line can always be tied back to the exact input/run.  Records capture
the package and dependency versions, the pipeline step, progress and the
criterion used for judgment.  Failures are logged as their concrete error
category -- exceptions are never collapsed into "success".
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
from importlib.metadata import version as _version
from pathlib import Path
from typing import Any

_RUN_LOGGER_NAME = "sparse_cholesky.runs"


def dependency_versions() -> dict[str, str]:
    """Versions of the runtime and numeric stack, recorded per run."""
    versions = {
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    for pkg in ("numpy", "scipy", "mpmath", "fastapi", "pydantic"):
        try:
            versions[pkg] = _version(pkg)
        except Exception:  # pragma: no cover
            versions[pkg] = "unknown"
    return versions


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


class RunLogger:
    """Append-only JSON Lines logger, safe for concurrent requests."""

    def __init__(self, log_dir: str | os.PathLike[str],
                 *, enabled: bool = True) -> None:
        self.log_dir = Path(log_dir)
        self.enabled = enabled
        self._lock = threading.Lock()
        if enabled:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, run_id: str) -> Path:
        date_part = run_id.split("-")[1] if "-" in run_id else "misc"
        return self.log_dir / f"run-{date_part}.jsonl"

    def event(self, run_id: str, step: str, status: str,
              **payload: Any) -> dict[str, Any]:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "run_id": run_id,
            "step": step,
            "status": status,
            "payload": payload,
        }
        if self.enabled:
            line = json.dumps(record, sort_keys=True,
                              default=_json_default) + "\n"
            with self._lock:
                with self._path_for(run_id).open("a", encoding="utf-8") as fh:
                    fh.write(line)
        return record

    def run_start(self, run_id: str, summary: dict[str, Any]) -> None:
        self.event(run_id, "run", "started",
                   versions=dependency_versions(), argv=sys.argv[:1],
                   **summary)

    def run_end(self, run_id: str, status: str, **payload: Any) -> None:
        self.event(run_id, "run", status, **payload)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if hasattr(obj, "tolist"):
        return obj.tolist()
    return repr(obj)


def configure_stdlib_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(_RUN_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"
        ))
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger
