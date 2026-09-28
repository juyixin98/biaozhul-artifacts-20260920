"""Pure weighted reduction of received buckets.

Extracted from the coordinator so the math is independently testable and
the state machine module stays focused on acceptance/barrier semantics.
The key correctness fact: workers report gradient *sums* over their local
batches, so the joint-batch mean is the sum of those gradient sums divided
by the true number of covering samples -- sample counts enter through the
denominator only, never as an extra multiply.
"""

from __future__ import annotations

import hashlib
from typing import Dict, List, Tuple

import numpy as np

from bucket_sync.bucketing import BucketLayout
from bucket_sync.round_types import BucketEvidence, Participant


def active_participants(participants: Dict[str, Participant]) -> List[Participant]:
    return [p for p in participants.values() if not p.skipped]


def reduce_buckets(
    layout: BucketLayout,
    active: List[Participant],
    generation: int,
    coverage_gaps: Dict[str, object],
) -> Tuple[Dict[int, np.ndarray], Tuple[BucketEvidence, ...]]:
    """Reduce every bucket over the active participants.

    ``coverage_gaps`` maps ``f"bucket_{i}"`` to gap detail for buckets where
    some trainable slot is not covered by every active worker; reduction is
    then performed per-slot over only the covering workers.
    """
    reduced: Dict[int, np.ndarray] = {}
    evidence: List[BucketEvidence] = []
    total_samples = int(sum(p.n_samples or 0 for p in active))
    contributors = tuple(p.worker_id for p in active)
    samples_per_worker = tuple(p.n_samples or 0 for p in active)

    for bucket in layout.buckets:
        acc = np.zeros(bucket.size, dtype=np.float64)
        sample_weight = np.zeros(bucket.size, dtype=np.float64)
        for part in active:
            seg = part.buckets[bucket.index]
            cov = part.coverage[bucket.index].astype(np.float64)
            acc += seg * cov
            sample_weight += cov * float(part.n_samples)
        ph = layout.placeholder_mask()[bucket.start : bucket.end]
        # In strict mode every trainable slot is fully covered, so a zero
        # weight can only occur on a placeholder slot; avoid divide-by-zero.
        safe_weight = np.where(sample_weight == 0.0, 1.0, sample_weight)
        acc = acc / safe_weight
        acc[ph] = 0.0
        reduced[bucket.index] = acc

        cover_counts = sum(
            part.coverage[bucket.index].astype(np.int64) for part in active
        )
        basis = (
            "mean over covering workers' samples: "
            "sum_w sum_gradient_w / sum_w n_w (per-slot coverage)"
            if coverage_gaps.get(f"bucket_{bucket.index}")
            else "joint-batch mean from per-worker gradient sums: "
            "sum_w sum_gradient_w / sum_w n_w"
        )
        evidence.append(
            BucketEvidence(
                bucket_index=bucket.index,
                generation=generation,
                contributors=contributors,
                samples_per_worker=samples_per_worker,
                total_samples=total_samples,
                reduced_norm=float(np.linalg.norm(acc)),
                reduced_hash12=hashlib.sha256(acc.tobytes()).hexdigest()[:12],
                weight_basis=basis,
                cover_min=int(cover_counts[~ph].min()) if np.any(~ph) else 0,
                cover_max=int(cover_counts[~ph].max()) if np.any(~ph) else 0,
                sample_weight_min=(
                    float(sample_weight[~ph].min()) if np.any(~ph) else 0.0
                ),
            )
        )
    return reduced, tuple(evidence)
