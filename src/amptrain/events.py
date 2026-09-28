"""Structured event log.

Every event carries:
* ``run_id``     -- the run it belongs to,
* ``window_index`` / ``micro_index`` -- progress coordinates,
* ``event``      -- the machine-readable step kind/verdict,
* ``version``    -- package and runtime versions,
* ``detail``     -- the *basis for the verdict* (which stage overflowed,
                    which tensor was non-finite, scale before/after, ...).

The log never maps exceptions or unknown states to success: skipped windows
are recorded as ``window_skipped`` and errors carry their error code.
"""

from __future__ import annotations

import json
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .version import VERSION

VERSION_CONTEXT = {
    "amptrain": VERSION,
    "python": platform.python_version(),
    "numpy": np.__version__,
}

EVENT_WINDOW_COMMITTED = "window_committed"
EVENT_WINDOW_SKIPPED = "window_skipped"
EVENT_RUN_CREATED = "run_created"
EVENT_CHECKPOINT_SAVED = "checkpoint_saved"
EVENT_CHECKPOINT_LOADED = "checkpoint_loaded"
EVENT_ERROR = "error"
EVENT_VALIDATION = "validation"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.dtype,)):
        return str(obj)
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


@dataclass(frozen=True)
class Event:
    seq: int
    run_id: str
    window_index: int
    event: str
    detail: dict[str, Any] = field(default_factory=dict)
    micro_index: int | None = None
    version: dict[str, str] = field(default_factory=lambda: dict(VERSION_CONTEXT))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventLog:
    """Append-only in-memory log; optionally mirrored to a JSONL file."""

    def __init__(self, run_id: str, sink: Path | None = None):
        self.run_id = run_id
        self._events: list[Event] = []
        self._sink = sink
        if sink is not None:
            sink.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        event: str,
        detail: dict[str, Any],
        *,
        window_index: int,
        micro_index: int | None = None,
    ) -> Event:
        record = Event(
            seq=len(self._events),
            run_id=self.run_id,
            window_index=window_index,
            micro_index=micro_index,
            event=event,
            detail=detail,
        )
        self._events.append(record)
        if self._sink is not None:
            with self._sink.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), default=_json_default) + "\n")
        return record

    def events(self) -> list[Event]:
        return list(self._events)

    def to_list(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events]
