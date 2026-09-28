"""Structured, run-correlated logging.

Every computation is executed under a :class:`RunIdentity` so that any log
line, stored record or failure can be traced back to the exact inputs and code
versions that produced it. Logs are JSON lines (one JSON object per line),
which keeps them machine-readable while remaining grep-able by run id.

A log record records the *judgement* (accept / reject / failure category), not
just "the call succeeded" -- an unknown or exceptional state is logged as
such and never smoothed into a success line.
"""
from __future__ import annotations

import json
import logging
import platform
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import numpy
import scipy


def dependency_versions() -> Dict[str, str]:
    import fastapi
    import pydantic

    return {
        "python": platform.python_version(),
        "numpy": numpy.__version__,
        "scipy": scipy.__version__,
        "fastapi": fastapi.__version__,
        "pydantic": pydantic.VERSION,
        "platform": platform.platform(),
    }


@dataclass(frozen=True)
class RunIdentity:
    run_id: str
    created_at: str
    purpose: str
    versions: Dict[str, str] = field(default_factory=dependency_versions)

    @classmethod
    def new(cls, purpose: str = "sample-size-plan") -> "RunIdentity":
        return cls(
            run_id=f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}",
            created_at=datetime.now(timezone.utc).isoformat(),
            purpose=purpose,
        )


class RunLogger:
    """Append-only JSONL logger keyed by a run id, with a human echo."""

    def __init__(self, run: RunIdentity, log_path: Optional[Path] = None, *, echo: bool = True):
        self.run = run
        self.log_path = Path(log_path) if log_path else None
        self.echo = echo
        self._py_logger = logging.getLogger(f"ssp.{run.run_id}")
        if not self._py_logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._py_logger.addHandler(handler)
            self._py_logger.setLevel(logging.INFO)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, record: Dict[str, Any]) -> None:
        record.setdefault("run_id", self.run.run_id)
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = json.dumps(record, sort_keys=True, default=_json_default)
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if self.echo:
            self._py_logger.info(line)

    def event(self, step: str, **payload: Any) -> None:
        self._write({"step": step, **payload})

    def input_recorded(self, label: str, payload: Dict[str, Any]) -> None:
        self._write({"step": "input", "label": label, "input": payload})

    def progress(self, message: str, **payload: Any) -> None:
        self._write({"step": "progress", "message": message, **payload})

    def judgement(self, verdict: str, reason: str, **payload: Any) -> None:
        if verdict not in {"accept", "reject", "failure", "error"}:
            raise ValueError(f"unknown verdict {verdict!r}")
        self._write({"step": "judgement", "verdict": verdict, "reason": reason, **payload})


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "value"):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, tuple)):
        return list(obj)
    raise TypeError(f"not JSON serialisable: {type(obj)}")
