"""Run journals: replayable diagnostics.

Every meaningful operation opens a :class:`RunJournal` with a unique,
time-sortable ``run_id``. A journal records:

* the request category and inputs (ids only, the corpus itself lives in SQLite),
* intermediate state (canonical forms, scored candidates, must-link blocks),
* each decision with a machine-readable reason,
* the terminal outcome or the exact error category.

Journals are appended as one JSON object per line (JSONL) under a logs
directory so a failing run can be reconstructed from ``run_id`` alone.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id(prefix: str = "run") -> str:
    # Sortable, collision-resistant without external deps.
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{_counter.next()}"


class _Counter:
    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._n += 1
            return self._n


_counter = _Counter()


@dataclass
class RunJournal:
    run_id: str
    operation: str
    log_dir: Path
    started_at: str = field(default_factory=_now)
    stages: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    status: str = "open"
    error: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.log_dir = Path(self.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.log_dir / f"{self.run_id}.jsonl"

    def stage(self, name: str, **state: Any) -> None:
        """Record an intermediate state snapshot (e.g. candidate scores)."""
        self.stages.append(
            {"at": _now(), "stage": name, "state": _jsonable(state)}
        )

    def decide(self, subject: str, rationale: str, **detail: Any) -> None:
        """Record a judgement and *why* (merge/split/reject/lock)."""
        self.decisions.append(
            {
                "at": _now(),
                "subject": subject,
                "rationale": rationale,
                "detail": _jsonable(detail),
            }
        )

    def fail(self, category: str, code: str, message: str, details: dict) -> None:
        self.status = "error"
        self.error = {
            "category": category,
            "code": code,
            "message": message,
            "details": _jsonable(details),
        }

    def succeed(self, **summary: Any) -> None:
        self.status = "ok"
        self.summary = _jsonable(summary)

    def flush(self) -> Path:
        """Write the whole journal as JSONL and return its path."""
        record = {
            "run_id": self.run_id,
            "operation": self.operation,
            "started_at": self.started_at,
            "finished_at": _now(),
            "status": self.status,
            "stages": self.stages,
            "decisions": self.decisions,
            "summary": getattr(self, "summary", None),
            "error": self.error,
        }
        # One JSON object per line per stage/decision, plus a terminal summary,
        # so the file is both line-tail-able and fully reconstructable.
        lines = [json.dumps(record, ensure_ascii=False, sort_keys=True)]
        with self.path.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
        return self.path


def _jsonable(value: Any) -> Any:
    """Convert frozensets/tuples and other non-JSON values deterministically."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=str)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (frozenset, set)):
        return [_jsonable(v) for v in sorted(value, key=str)]
    if isinstance(value, float):
        return round(value, 6)
    return value
