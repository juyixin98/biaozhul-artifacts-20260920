"""Timestamp association between estimated and ground-truth trajectories.

For every estimated pose the nearest ground-truth pose in time is kept if the
absolute time difference is within ``max_time_diff``.  Unmatched poses are
dropped from the statistics (never zero-padded), and the response reports how
much of each track was actually used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import EvaluationError
from .geometry import as_vec3, normalize_quat, quat_to_rot


def _as_vec4(v: Any, what: str) -> np.ndarray:
    arr = np.asarray(v, dtype=np.float64)
    if arr.shape != (4,):
        raise EvaluationError(
            "INVALID_POSE", f"{what} must be a 4-element array.", {"got_shape": list(arr.shape)}
        )
    if not np.all(np.isfinite(arr)):
        raise EvaluationError("INVALID_POSE", f"{what} contains non-finite values.")
    return arr


@dataclass(frozen=True)
class Track:
    """A parsed trajectory: per-pose timestamps, translations and rotations."""

    times: np.ndarray  # (N,)
    positions: np.ndarray  # (N, 3)
    rotations: np.ndarray  # (N, 3, 3)

    def __len__(self) -> int:
        return int(self.times.shape[0])


def build_track(poses: list, label: str) -> Track:
    """Parse API pose objects, validating times and quaternions."""
    if len(poses) == 0:
        raise EvaluationError("EMPTY_TRAJECTORY", f"The {label} trajectory is empty.")

    times = np.empty(len(poses), dtype=np.float64)
    positions = np.empty((len(poses), 3), dtype=np.float64)
    rotations = np.empty((len(poses), 3, 3), dtype=np.float64)

    for i, p in enumerate(poses):
        t = float(p["time"]) if not hasattr(p, "time") else float(p.time)
        pos = p["position"] if not hasattr(p, "position") else p.position
        quat = p["quaternion_xyzw"] if not hasattr(p, "quaternion_xyzw") else p.quaternion_xyzw
        if not np.isfinite(t):
            raise EvaluationError(
                "INVALID_TIMESTAMP",
                f"{label} pose index {i} has a non-finite timestamp.",
                {"index": i},
            )
        times[i] = t
        positions[i] = as_vec3(pos, f"{label} pose index {i} position")
        q = _as_vec4(quat, f"{label} pose index {i} quaternion")
        rotations[i] = quat_to_rot(normalize_quat(q))

    order = np.argsort(times, kind="stable")
    times_sorted = times[order]
    dup = np.flatnonzero(np.diff(times_sorted) == 0.0)
    if dup.size > 0:
        first_dup = float(times_sorted[dup[0]])
        raise EvaluationError(
            "DUPLICATE_TIMESTAMP",
            f"Duplicate timestamp {first_dup:.9g} found in the {label} trajectory; "
            "each pose time must be unique.",
            {"label": label, "timestamp": first_dup},
        )

    return Track(times=times_sorted, positions=positions[order], rotations=rotations[order])


@dataclass(frozen=True)
class Association:
    """Matched pose pairs, already sorted by ground-truth time."""

    est_idx: np.ndarray  # indices into the *sorted* estimated track
    gt_idx: np.ndarray  # indices into the *sorted* ground-truth track
    est_times: np.ndarray
    gt_times: np.ndarray
    time_diffs: np.ndarray


def associate_tracks(est: Track, gt: Track, max_time_diff: float) -> Association:
    """Nearest-ground-truth matching within the time window."""
    est_idx: list[int] = []
    gt_idx: list[int] = []

    # Ground-truth times are sorted: searchsorted gives the insertion point.
    for i, te in enumerate(est.times):
        k = int(np.searchsorted(gt.times, te))
        candidates: list[int] = []
        if k < len(gt):
            candidates.append(k)
        if k > 0:
            candidates.append(k - 1)
        best = min(candidates, key=lambda j: (abs(gt.times[j] - te), j))
        dt = abs(float(gt.times[best]) - float(te))
        if dt <= max_time_diff:
            est_idx.append(i)
            gt_idx.append(best)

    if not est_idx:
        raise EvaluationError(
            "NO_MATCHES",
            "No estimated pose could be matched to ground truth within "
            f"max_time_diff={max_time_diff:g} s; ATE/RPE cannot be computed.",
            {"max_time_diff": max_time_diff},
        )

    ei = np.asarray(est_idx, dtype=np.int64)
    gi = np.asarray(gt_idx, dtype=np.int64)

    # A ground-truth pose must not be paired with two different estimates.
    # Keep the closest estimate for each ground-truth index (ties: earlier one).
    order = np.lexsort((np.abs(est.times[ei] - gt.times[gi]), gi))
    ei, gi = ei[order], gi[order]
    _, first = np.unique(gi, return_index=True)
    ei, gi = ei[first], gi[first]

    # Sort the final pairs chronologically by ground-truth time.
    chrono = np.argsort(gi, kind="stable")
    ei, gi = ei[chrono], gi[chrono]

    return Association(
        est_idx=ei,
        gt_idx=gi,
        est_times=est.times[ei],
        gt_times=gt.times[gi],
        time_diffs=np.abs(est.times[ei] - gt.times[gi]),
    )
