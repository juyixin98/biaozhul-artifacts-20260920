"""Structured run logging.

Every solve emits JSON-lines events keyed by run_id so a failing run can be
replayed and audited: the normalized input, kernel milestones, intermediate
iteration state, and the final status decision together with its reason.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional


class RunLogger:
    def __init__(self, log_dir: str = "logs", filename: str = "runs.jsonl") -> None:
        self.log_dir = log_dir
        self.path = os.path.join(log_dir, filename)
        self._lock = threading.Lock()
        os.makedirs(log_dir, exist_ok=True)

    def log(self, run_id: str, event: str, **fields: Any) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "run_id": run_id,
            "event": event,
            **fields,
        }
        line = json.dumps(record, default=_json_safe, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")


def _json_safe(value: Any) -> Any:
    if isinstance(value, complex):
        return {"re": value.real, "im": value.imag}
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return repr(value)
    return repr(value)


class NullLogger:
    """For tests that do not care about log output."""

    def log(self, run_id: str, event: str, **fields: Any) -> None:  # noqa: D102
        pass
