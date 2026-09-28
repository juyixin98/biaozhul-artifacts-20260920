"""Structured diagnostics with redaction.

Every accept / reject / cannot-decide outcome in the coordinator produces
a :class:`DiagnosticEvent` carrying a record id, the round, the worker,
the reason, and a small amount of key state.  Events are the audit trail
that answers "why was this submission accepted, rejected, or left
undecided".

Redaction policy: gradient *values* and parameter *values* are treated as
sensitive.  Events record shapes, norms, counts, and hashes — never raw
arrays.  Only non-sensitive scalars (round ids, bucket indices, sample
counts, reasons) appear verbatim.
"""

from __future__ import annotations

import hashlib
import itertools
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np


def _redact_value(value: Any) -> Any:
    """Recursively redact a value for safe logging."""
    if isinstance(value, np.ndarray):
        arr = np.asarray(value, dtype=np.float64)
        return {
            "kind": "ndarray",
            "shape": list(arr.shape),
            "l2_norm": round(float(np.linalg.norm(arr)), 6) if arr.size else 0.0,
            "sha256_12": hashlib.sha256(arr.tobytes()).hexdigest()[:12],
        }
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def redact_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """Redact a state snapshot; arrays become shape/norm/hash summaries."""
    return _redact_value(state)


@dataclass(frozen=True)
class DiagnosticEvent:
    """One auditable decision or observation."""

    record_id: str
    round_id: Optional[int]
    worker_id: Optional[str]
    outcome: str  # "accepted" | "rejected" | "undecided" | "info"
    reason: str
    detail: Dict[str, Any] = field(default_factory=dict)


class Diagnostics:
    """Thread-safe in-memory sink for diagnostic events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = itertools.count(1)
        self._events: List[DiagnosticEvent] = []

    def emit(
        self,
        *,
        round_id: Optional[int],
        worker_id: Optional[str],
        outcome: str,
        reason: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> DiagnosticEvent:
        with self._lock:
            record_id = f"rec-{next(self._counter):06d}"
            event = DiagnosticEvent(
                record_id=record_id,
                round_id=round_id,
                worker_id=worker_id,
                outcome=outcome,
                reason=reason,
                detail=redact_state(detail or {}),
            )
            self._events.append(event)
            return event

    def events(self) -> List[DiagnosticEvent]:
        with self._lock:
            return list(self._events)

    def for_round(self, round_id: int) -> List[DiagnosticEvent]:
        return [e for e in self.events() if e.round_id == round_id]

    def rejections(self) -> List[DiagnosticEvent]:
        return [e for e in self.events() if e.outcome == "rejected"]
