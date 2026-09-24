"""Append-only audit log.

Every accepted and rejected command decision is written as one JSON object
per line (JSONL), including the *reason* a command was dropped — expired,
rollback, unauthorized, unknown robot, topic-escape, etc. This file is the
durable record; the in-process event bus (events.py) handles live
subscriptions.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any


class AuditLog:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._fh = None
        if path:
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
            # Line-buffered so tail -f works while the gateway runs.
            self._fh = open(path, "a", buffering=1, encoding="utf-8")

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        entry = {"ts": time.time(), "event": event, **fields}
        line = json.dumps(entry, separators=(",", ":"), sort_keys=True,
                          ensure_ascii=False)
        with self._lock:
            if self._fh is not None:
                self._fh.write(line + "\n")
        return entry

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


def read_jsonl(path: str) -> list[dict[str, Any]]:
    """Helper used by tests and tooling to read an audit log back."""
    out: list[dict[str, Any]] = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
