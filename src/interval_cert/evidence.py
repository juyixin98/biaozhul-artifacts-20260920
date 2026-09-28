"""Evidence records: every kernel decision leaves a replayable trace.

The trace is the audit log of a run: each record carries the run id, the
step number, the action taken, the interval it applied to, and the reason.
Certified roots additionally carry the full theorem witness (the interval
Newton image, the derivative enclosure, the containment margins) so the
certification can be re-checked independently of the kernel code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .intervals import Interval, interval_to_dict, interval_to_str


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"run-{stamp}-{uuid.uuid4().hex[:8]}"


@dataclass
class TraceRecord:
    run_id: str
    step: int
    action: str  # eliminate | bisect | contract | certify | undecided | ...
    interval: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "step": self.step,
            "action": self.action,
            "interval": self.interval,
            "reason": self.reason,
            "details": self.details,
        }


@dataclass
class RootWitness:
    """Theorem witness for one interval Newton certification step."""

    theorem: str  # "interval_newton_contraction"
    initial_interval: dict[str, str]
    derivative_interval: dict[str, str]
    f_at_midpoint: dict[str, str]
    newton_image: dict[str, str]
    containment_margin_lo: str
    containment_margin_hi: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "theorem": self.theorem,
            "initial_interval": self.initial_interval,
            "derivative_interval": self.derivative_interval,
            "f_at_midpoint": self.f_at_midpoint,
            "newton_image": self.newton_image,
            "containment_margin_lo": self.containment_margin_lo,
            "containment_margin_hi": self.containment_margin_hi,
        }


@dataclass
class CertifiedRoot:
    """One certified unique root.

    ``witnesses`` normally holds a single Newton witness; when two
    neighbouring boxes certify the same root (a root exactly on a bisection
    boundary), their enclosures are merged and every witness is retained.
    """

    interval: Interval
    witnesses: list[RootWitness]
    tightening_steps: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "interval": interval_to_dict(self.interval),
            "evidence": {
                "theorem": "interval_newton_contraction",
                "witnesses": [w.to_dict() for w in self.witnesses],
                "tightening_steps": self.tightening_steps,
                "final_interval": interval_to_dict(self.interval),
            },
        }


@dataclass
class UndecidedInterval:
    interval: Interval
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"interval": interval_to_dict(self.interval), "reason": self.reason}


class TraceLog:
    """Collects trace records for one run; also mirrors them to a sink."""

    def __init__(self, run_id: str, sink=None):
        self.run_id = run_id
        self.records: list[TraceRecord] = []
        self._sink = sink

    def record(
        self,
        step: int,
        action: str,
        interval: Interval,
        reason: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        record = TraceRecord(
            run_id=self.run_id,
            step=step,
            action=action,
            interval=interval_to_str(interval),
            reason=reason,
            details=details or {},
        )
        self.records.append(record)
        if self._sink is not None:
            self._sink(record.to_dict())

    def to_list(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.records]
