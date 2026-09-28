"""Error evidence: the auditable trail of one expv evaluation.

Every accepted or rejected step appends a :class:`StepEvidence`; the
aggregate :class:`ExpvEvidence` is what the service layer serialises into
responses and logs.  Nothing here computes anything -- it only records.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy
import scipy

import krylov_expv


@dataclass(frozen=True)
class StepEvidence:
    """One time-segment decision."""

    index: int
    t_before: float
    tau: float
    krylov_dim: int
    subspace_residual: float
    error_estimate: float
    accepted: bool
    halvings: int
    happy_breakdown: bool


@dataclass
class ExpvEvidence:
    """Aggregate evidence for a whole request."""

    termination: str = "converged"
    failure_category: str | None = None
    failure_message: str | None = None
    steps: list[StepEvidence] = field(default_factory=list)
    total_error_estimate: float = 0.0
    max_subspace_residual: float = 0.0
    memory_bytes_used: int = 0
    started_at: float = field(default_factory=time.perf_counter)
    elapsed_ms: float = 0.0
    versions: dict[str, str] = field(
        default_factory=lambda: {
            "krylov_expv": krylov_expv.__version__,
            "numpy": numpy.__version__,
            "scipy": scipy.__version__,
        }
    )

    @property
    def num_steps(self) -> int:
        return sum(1 for s in self.steps if s.accepted)

    def finish(self) -> None:
        self.elapsed_ms = (time.perf_counter() - self.started_at) * 1.0e3

    def record_failure(self, category: str, message: str) -> None:
        self.termination = "failed"
        self.failure_category = category
        self.failure_message = message
        self.finish()
