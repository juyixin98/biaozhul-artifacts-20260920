"""Run logging: every differentiable operation gets a ``run_id`` and a JSONL
record containing enough intermediate state to replay the judgement.

Records are appended to ``logs/hvp_runs.jsonl`` (path configurable via
``HVP_LOG_PATH``) and retained in memory (last ``keep`` records).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_DEFAULT_LOG_PATH = Path(
    os.environ.get("HVP_LOG_PATH", "logs/hvp_runs.jsonl")
).expanduser()


class RunLogger:
    def __init__(self, path: Path | str = _DEFAULT_LOG_PATH,
                 keep: int = 512) -> None:
        self.path = Path(path)
        self.keep = keep
        self._lock = threading.RLock()
        self._recent: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def new_run_id(self) -> str:
        return f"run-{uuid.uuid4().hex[:16]}"

    def record(
        self,
        run_id: str,
        operation: str,
        *,
        state_id: str | None = None,
        status: str,
        request_summary: dict[str, Any] | None = None,
        intermediates: dict[str, Any] | None = None,
        result_summary: dict[str, Any] | None = None,
        checks: list[dict[str, Any]] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "run_id": run_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "monotonic_s": round(time.monotonic(), 6),
            "operation": operation,
            "state_id": state_id,
            "status": status,
            "request_summary": request_summary or {},
            "intermediates": intermediates or {},
            "result_summary": result_summary or {},
            "checks": checks or [],
            "error": error,
        }
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False,
                                    default=_json_default) + "\n")
            self._recent[run_id] = entry
            self._order.append(run_id)
            while len(self._order) > self.keep:
                old = self._order.pop(0)
                self._recent.pop(old, None)
        return entry

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._recent.get(run_id)
            if entry is not None:
                return entry
        # fall back to scanning the log file (replays older runs)
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("run_id") == run_id:
                        return obj
        return None


def _json_default(obj: Any) -> Any:
    import numpy as np

    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    return str(obj)
