"""Structured, replay-capable run logs.

Every planner/executor action appends a JSONL event carrying a run id,
monotonic sequence number, timestamp, event kind and the key intermediate state
plus the reason for any judgment. A failed run is therefore replayable from its
log: graph spec, concrete shapes, plan peaks and the failing category are all on
record.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


@dataclass
class LogEvent:
    run_id: str
    seq: int
    ts: float
    kind: str
    message: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "message": self.message,
            "data": _json_safe(self.data),
        }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


class RunLogger:
    """Append-only JSONL logger; safe to share across threads."""

    def __init__(
        self,
        run_id: str | None = None,
        path: str | os.PathLike[str] | None = None,
        echo: bool = False,
    ) -> None:
        self.run_id = run_id or new_run_id()
        self._lock = threading.Lock()
        self._seq = 0
        self.events: list[LogEvent] = []
        self._fh: TextIO | None = None
        self.path: Path | None = None
        if path is not None:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        self.echo = echo

    def event(
        self, kind: str, message: str, run_id: str | None = None, **data: Any
    ) -> LogEvent:
        effective_run = run_id or self.run_id
        with self._lock:
            self._seq += 1
            ev = LogEvent(
                run_id=effective_run,
                seq=self._seq,
                ts=time.time(),
                kind=kind,
                message=message,
                data=data,
            )
            self.events.append(ev)
            line = json.dumps(ev.to_dict(), ensure_ascii=False)
            if self._fh is not None:
                self._fh.write(line + "\n")
                self._fh.flush()
        if self.echo:
            print(line)
        return ev

    def failure(
        self,
        category: str,
        message: str,
        run_id: str | None = None,
        **data: Any,
    ) -> LogEvent:
        return self.event(
            "failure", message, category=category, run_id=run_id, **data
        )

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def lines(self) -> list[str]:
        return [json.dumps(e.to_dict(), ensure_ascii=False) for e in self.events]
