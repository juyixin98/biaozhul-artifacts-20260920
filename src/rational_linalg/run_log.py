"""Run logging: unique run ids and append-only JSONL event logs.

Every request gets a ``run_id`` (timestamp + short random suffix).  Kernel and
solver emit structured events into the run's event list; the full list is
flushed to ``<log_dir>/runs.jsonl`` and can be reconstructed from the file
alone -- exactly what is needed to replay a failure (run number, key
intermediate states, decision reasons).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _default_log_dir() -> Path:
    return Path(
        os.environ.get("RATIONAL_LINALG_LOG_DIR", "logs")
    ).resolve()


def new_run_id(kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{kind}-{stamp}-{uuid.uuid4().hex[:8]}"


class RunRecorder:
    """Collects events for one run and persists them to JSONL."""

    def __init__(self, run_id: str, kind: str, log_dir: Path | None = None):
        self.run_id = run_id
        self.kind = kind
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.events: list[dict[str, Any]] = []
        self.status = "running"
        self.result: dict[str, Any] | None = None
        self.error: dict[str, Any] | None = None
        self._log_dir = log_dir or _default_log_dir()

    def emit(self, event: dict[str, Any]) -> None:
        record = {
            "run_id": self.run_id,
            "at": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        self.events.append(record)

    def complete(self, result: dict[str, Any]) -> None:
        self.status = "completed"
        self.result = result

    def fail(self, error: dict[str, Any]) -> None:
        self.status = "failed"
        self.error = error

    def flush(self) -> Path:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        path = self._log_dir / "runs.jsonl"
        header = {
            "type": "run_summary",
            "run_id": self.run_id,
            "kind": self.kind,
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": self.status,
            "result": self.result,
            "error": self.error,
        }
        with path.open("a", encoding="utf-8") as handle:
            for event in self.events:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.write(json.dumps(header, sort_keys=True) + "\n")
        return path


class RunRegistry:
    """In-process registry so a run's progress can be fetched by id."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecorder] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, run_id: str | None = None) -> RunRecorder:
        rid = run_id or new_run_id(kind)
        recorder = RunRecorder(rid, kind)
        with self._lock:
            if rid in self._runs:
                from .errors import StateConflictError

                raise StateConflictError(
                    f"run_id {rid!r} already exists in this process; "
                    f"choose a different run_id",
                    code="RUN_ID_CONFLICT",
                    details={"run_id": rid, "existing_status": self._runs[rid].status},
                )
            self._runs[rid] = recorder
        return recorder

    def get(self, run_id: str) -> RunRecorder | None:
        with self._lock:
            return self._runs.get(run_id)

    def summary(self, recorder: RunRecorder) -> dict[str, Any]:
        return {
            "run_id": recorder.run_id,
            "kind": recorder.kind,
            "status": recorder.status,
            "started_at": recorder.started_at,
            "event_count": len(recorder.events),
            "events": recorder.events,
            "result": recorder.result,
            "error": recorder.error,
        }
