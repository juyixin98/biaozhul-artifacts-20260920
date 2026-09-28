"""Replayable JSONL run log.

One file per working directory, one JSON object per line. Every run records:

* ``run_id`` / ``ts`` - stable identifier and UTC timestamp of the event;
* ``stage`` - one of ``RECEIVED``, ``PARSED``, ``VALIDATED``, ``SEARCH``,
  ``EXECUTED``, ``COMPLETED``, ``FAILED``;
* ``category`` / ``code`` - the error contract classification on failure;
* key intermediate state (ground action count, search status, expanded /
  generated counters, bound hit, executor failure step and violated literals);
* ``reason`` - the human-readable judgement why the run ended the way it did.

The log is append-only; a crash leaves every previously flushed line intact
and the corresponding run row in SQLite points back at the same id, so a
problem can be reconstructed from either source.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RunLogger:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def event(
        self,
        run_id: str,
        stage: str,
        *,
        reason: str | None = None,
        category: str | None = None,
        code: str | None = None,
        **fields: Any,
    ) -> None:
        record: dict[str, Any] = {
            "run_id": run_id,
            "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "stage": stage,
        }
        if category is not None:
            record["category"] = category
        if code is not None:
            record["code"] = code
        if reason is not None:
            record["reason"] = reason
        record.update(_json_safe(fields))
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + os.linesep)
                handle.flush()

    def read(self, run_id: str) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("run_id") == run_id:
                    out.append(record)
        return out


def _json_safe(value: Any) -> Any:
    """Coerce search/evidence values into JSON-compatible primitives."""

    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and value != value:
        return "NaN"
    if isinstance(value, float) and value in (float("inf"), float("-inf")):
        return "Infinity" if value > 0 else "-Infinity"
    return value
