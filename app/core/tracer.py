"""Structured run tracing.

Each certification run owns a ``Tracer`` with a stable ``run_id``. Events
record the key intermediate state (interval bounds, evaluation ranges,
decision reason) so that any reported result can be replayed.

Logs are emitted as JSONL under the directory given to the tracer; the same
events are returned in-memory on the response (truncated) and written to disk
(full) for post-mortem inspection.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# Events held in-memory per response are capped so a 200k-evaluation run
# cannot blow up the HTTP payload; the on-disk log keeps every event.
MAX_IN_MEMORY_EVENTS = 400


@dataclass
class Tracer:
    run_id: str
    log_dir: str | None
    start_time: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    _fh: Any = None
    _dropped: int = 0

    @staticmethod
    def create(log_dir: str | None) -> "Tracer":
        run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        tracer = Tracer(run_id=run_id, log_dir=log_dir)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, f"run-{run_id}.jsonl")
            tracer._fh = open(path, "w", encoding="utf-8")
        return tracer

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    def event(self, stage: str, reason: str, **state: Any) -> None:
        """Record one decision point.

        ``stage`` is the algorithm phase, ``reason`` is the machine-readable
        justification for the branch taken; remaining kwargs are intermediate
        state. Non-JSON-serialisable values are coerced via ``str``.
        """
        self.count(f"event:{stage}")
        record = {
            "seq": len(self.events) + self._dropped,
            "elapsed_ms": round((time.time() - self.start_time) * 1000, 3),
            "stage": stage,
            "reason": reason,
            "state": state,
        }
        if self._fh is not None:
            self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if len(self.events) < MAX_IN_MEMORY_EVENTS:
            self.events.append(record)
        else:
            self._dropped += 1

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "elapsed_ms": round((time.time() - self.start_time) * 1000, 3),
            "counters": dict(self.counters),
            "events": list(self.events),
            "events_returned": len(self.events),
            "events_on_disk": len(self.events) + self._dropped,
            "log_path": (
                os.path.join(self.log_dir, f"run-{self.run_id}.jsonl")
                if self.log_dir
                else None
            ),
        }

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "Tracer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
