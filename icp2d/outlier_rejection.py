"""Outlier rejection for ICP correspondences.

Given nearest-neighbour distances for all correspondences, decide which ones
are inliers. Four explicit, composable strategies are provided:

- ``none``:     keep everything (no rejection).
- ``threshold``:drop correspondences whose distance exceeds ``max_distance``.
- ``trimmed``:  keep only the ``trim_ratio`` fraction of correspondences with
                the smallest distances (robust to partial overlap).
- ``mad``:      adaptive gate at ``median + mad_scale * MAD`` of the
                distances, additionally capped by ``max_distance``.

Every strategy is also bounded by the absolute ``max_distance`` gate (except
``none``), so grossly wrong associations never enter the solver.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

VALID_STRATEGIES = ("threshold", "trimmed", "mad", "none")


@dataclass
class RejectionResult:
    """Outcome of one rejection pass."""

    inlier_mask: np.ndarray  # bool array over correspondences
    threshold_used: float  # distance gate actually applied (inf = no gate)
    strategy: str


def reject_outliers(
    distances: np.ndarray,
    strategy: str = "mad",
    max_distance: float = 0.5,
    trim_ratio: float = 0.8,
    mad_scale: float = 3.0,
) -> RejectionResult:
    """Classify correspondences as inlier/outlier from their NN distances."""
    distances = np.asarray(distances, dtype=float)
    if strategy not in VALID_STRATEGIES:
        raise ValueError(
            f"unknown rejection strategy {strategy!r}; "
            f"expected one of {VALID_STRATEGIES}"
        )
    if not 0.0 < trim_ratio <= 1.0:
        raise ValueError("trim_ratio must be in (0, 1]")
    if max_distance <= 0:
        raise ValueError("max_distance must be positive")

    if strategy == "none":
        return RejectionResult(
            inlier_mask=np.ones(len(distances), dtype=bool),
            threshold_used=float("inf"),
            strategy=strategy,
        )

    if strategy == "threshold":
        gate = max_distance
    elif strategy == "trimmed":
        keep = max(1, int(np.ceil(trim_ratio * len(distances))))
        kth = np.sort(distances)[keep - 1]
        gate = min(kth, max_distance)
    else:  # "mad"
        median = float(np.median(distances))
        mad = float(np.median(np.abs(distances - median)))
        # 1.4826 scales MAD to a std-deviation estimate for normal data.
        adaptive = median + mad_scale * 1.4826 * mad
        # Degenerate case: all distances (nearly) identical -> MAD == 0 and the
        # adaptive gate would reject nothing. Fall back to the absolute gate.
        if adaptive <= median:
            adaptive = np.inf
        gate = min(adaptive, max_distance)

    return RejectionResult(
        inlier_mask=distances <= gate,
        threshold_used=float(gate),
        strategy=strategy,
    )
