"""Structured error evidence returned alongside every result."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class SegmentEvidence:
    """Evidence for one time segment (possibly merged from restart halves)."""

    segment_index: int
    step_size: float
    krylov_dim: int
    subspace_residual_norm: float
    error_estimate: float
    restart_splits: int
    matvec_count: int
    converged: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ErrorEvidence:
    """Whole-request evidence: plan, per-segment records, cumulative estimate."""

    norm_a_1: float
    planned_segments: int
    segments: list[SegmentEvidence] = field(default_factory=list)
    cumulative_error_estimate: float = 0.0
    total_matvec_count: int = 0
    trivial_case: Optional[str] = None

    def add_segment(self, segment: SegmentEvidence) -> None:
        self.segments.append(segment)
        self.cumulative_error_estimate += segment.error_estimate
        self.total_matvec_count += segment.matvec_count

    def to_dict(self) -> dict:
        return {
            "norm_a_1": self.norm_a_1,
            "planned_segments": self.planned_segments,
            "segments": [s.to_dict() for s in self.segments],
            "cumulative_error_estimate": self.cumulative_error_estimate,
            "total_matvec_count": self.total_matvec_count,
            "trivial_case": self.trivial_case,
        }
