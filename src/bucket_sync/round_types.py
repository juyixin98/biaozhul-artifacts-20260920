"""Value types, failure categories, and internal state for one training round.

Split out of :mod:`bucket_sync.coordinator` so the state machine module
stays under the maintainability ceiling and so these plain types can be
imported without pulling in the coordinator.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple

import numpy as np


class RejectReason(str, Enum):
    """Machine-readable failure categories for rejected calls."""

    UNKNOWN_WORKER = "unknown_worker"
    WORKER_LOST = "worker_lost"
    NOT_PARTICIPANT = "not_participant"
    ROUND_NOT_OPEN = "round_not_open"
    ROUND_ABORTED = "round_aborted"
    ROUND_COMMITTED = "round_committed"
    WRONG_ROUND = "wrong_round"
    WRONG_BASE_GENERATION = "wrong_base_generation"
    UNKNOWN_BUCKET = "unknown_bucket"
    BAD_SHAPE = "bad_shape"
    NON_FINITE = "non_finite"
    INVALID_SAMPLE_COUNT = "invalid_sample_count"
    SAMPLE_COUNT_MISMATCH = "sample_count_mismatch"
    DUPLICATE_BUCKET = "duplicate_bucket"
    PLACEHOLDER_NONZERO = "placeholder_nonzero"
    BAD_MASK = "bad_mask"
    MISSING_GRADIENT = "missing_gradient"
    INCOMPLETE_SUBMISSION = "incomplete_submission"
    WORKER_SKIPPED_ROUND = "worker_skipped_round"


class RoundStatus(str, Enum):
    OPEN = "open"
    COMMITTED = "committed"
    ABORTED = "aborted"


@dataclass(frozen=True)
class SubmissionResult:
    accepted: bool
    reason: Optional[str]
    record_id: str
    bucket_index: int
    bucket_complete: bool
    all_buckets_received: bool


@dataclass(frozen=True)
class BucketEvidence:
    """Reduction evidence for one bucket (what the reducer based its result on)."""

    bucket_index: int
    generation: int
    contributors: Tuple[str, ...]
    samples_per_worker: Tuple[int, ...]
    total_samples: int
    reduced_norm: float
    reduced_hash12: str
    weight_basis: str  # human-readable description of the weighting used
    cover_min: int = 0
    cover_max: int = 0
    sample_weight_min: float = 0.0


@dataclass(frozen=True)
class CommitReport:
    outcome: str  # "committed" | "rejected" | "undecided"
    reason: Optional[str]
    record_id: str
    round_id: int
    generation_before: int
    generation_after: Optional[int]
    bucket_evidence: Tuple[BucketEvidence, ...]
    detail: Dict[str, object]


@dataclass
class Worker:
    worker_id: str
    alive: bool = True
    last_heartbeat: float = 0.0
    lost_reason: Optional[str] = None


@dataclass
class Participant:
    worker_id: str
    n_samples: Optional[int] = None
    buckets: Dict[int, np.ndarray] = field(default_factory=dict)
    coverage: Dict[int, np.ndarray] = field(default_factory=dict)
    skipped: bool = False


@dataclass
class Round:
    round_id: int
    generation: int
    base_params: Dict[str, np.ndarray]
    base_token: str
    participants: Dict[str, Participant]
    status: RoundStatus = RoundStatus.OPEN
    abort_reason: Optional[str] = None
    reduced: Dict[int, np.ndarray] = field(default_factory=dict)
    evidence: Tuple[BucketEvidence, ...] = ()
    undecided_logged: Dict[str, str] = field(default_factory=dict)


def params_token(generation: int, params: Dict[str, np.ndarray]) -> str:
    """Stable token identifying the exact base weights of a round."""
    h = hashlib.sha256()
    h.update(f"gen={generation}".encode())
    for name in sorted(params):
        h.update(name.encode())
        h.update(np.asarray(params[name], dtype=np.float64).tobytes())
    return f"g{generation}-{h.hexdigest()[:16]}"
