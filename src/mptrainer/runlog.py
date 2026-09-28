"""Structured run logging: one JSON object per line, correlated by run_id.

Every record carries the software versions, the run identity, a monotonic
sequence number, and the decision basis (overflow flag, scale before/after,
commit/skip reason) so test logs and demo output can be traced back to the
exact input batch and training step that produced them.
"""

from __future__ import annotations

import json
import platform
import time
from pathlib import Path
from typing import Any

import numpy as np

from .version import __version__


def runtime_versions() -> dict[str, str]:
    return {
        "mptrainer": __version__,
        "python": platform.python_version(),
        "numpy": np.__version__,
    }


class RunLogger:
    """Append-only JSONL logger scoped to a single run."""

    def __init__(self, run_id: str, log_dir: str | Path | None = None) -> None:
        self.run_id = run_id
        self._seq = 0
        self._records: list[dict[str, Any]] = []
        self._path: Path | None = None
        if log_dir is not None:
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            self._path = directory / f"{run_id}.jsonl"

    @property
    def records(self) -> list[dict[str, Any]]:
        return list(self._records)

    def log(self, event: str, **payload: Any) -> dict[str, Any]:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": time.time(),
            "run_id": self.run_id,
            "event": event,
            "versions": runtime_versions(),
            **payload,
        }
        self._records.append(record)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=_json_default) + "\n")
        return record


def _json_default(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    return str(value)
