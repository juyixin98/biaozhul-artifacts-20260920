"""Trajectory containers and timestamp association."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import (
    DuplicateTimestampError,
    InvalidOrientationError,
    InvalidRequestError,
    ZeroMatchesError,
)
from .rotation import quat_to_matrix


@dataclass(frozen=True)
class Trajectory:
    """A time-ordered sequence of poses.

    Attributes:
        times: strictly increasing timestamps, shape (n,)
        positions: shape (n, 3)
        rotations: shape (n, 3, 3), world-from-body rotation matrices
        quaternions: original normalized ``[w, x, y, z]`` input, shape (n, 4)
    """

    times: np.ndarray
    positions: np.ndarray
    rotations: np.ndarray
    quaternions: np.ndarray

    def __len__(self) -> int:
        return int(self.times.shape[0])


def build_trajectory(poses: list[dict], label: str) -> Trajectory:
    """Validate raw ``{"timestamp", "position", "orientation"}`` dicts.

    Timestamps need not arrive sorted, but must be unique after sorting:
    a repeated timestamp is ambiguous (which pose is the pose?) so it is
    rejected explicitly instead of keeping one copy arbitrarily.
    """
    if not poses:
        raise InvalidRequestError(f"{label} trajectory is empty")

    times = np.empty(len(poses), dtype=np.float64)
    positions = np.empty((len(poses), 3), dtype=np.float64)
    quats = np.empty((len(poses), 4), dtype=np.float64)

    for i, p in enumerate(poses):
        try:
            times[i] = float(p["timestamp"])
            pos = p["position"]
            quat = p["orientation"]
            positions[i] = [float(pos[0]), float(pos[1]), float(pos[2])]
            quats[i] = [float(quat[0]), float(quat[1]),
                        float(quat[2]), float(quat[3])]
        except (KeyError, TypeError, IndexError, ValueError) as exc:
            raise InvalidRequestError(
                f"{label} pose {i}: expected timestamp (number), "
                "position [x,y,z], orientation [w,x,y,z]"
            ) from exc
        if not np.all(np.isfinite([times[i], *positions[i], *quats[i]])):
            raise InvalidRequestError(
                f"{label} pose {i}: non-finite timestamp/position/orientation"
            )
        norm = float(np.linalg.norm(quats[i]))
        if norm < 1e-12:
            raise InvalidOrientationError(
                f"{label} pose at t={times[i]}: zero-norm quaternion"
            )
        quats[i] = quats[i] / norm  # store normalized
        if quats[i, 0] < 0.0:
            quats[i] = -quats[i]

    order = np.argsort(times, kind="stable")
    times = times[order]
    positions = positions[order]
    quats = quats[order]

    duplicates = np.diff(times) <= 0.0
    if np.any(duplicates):
        first_dup = float(times[1:][np.argmax(duplicates)])
        raise DuplicateTimestampError(
            f"{label} trajectory contains a repeated timestamp "
            f"(first duplicate at t={first_dup})"
        )

    rotations = np.empty((len(poses), 3, 3), dtype=np.float64)
    for i in range(len(poses)):
        rotations[i] = quat_to_matrix(quats[i])

    return Trajectory(
        times=times, positions=positions, rotations=rotations, quaternions=quats
    )


@dataclass(frozen=True)
class MatchedPairs:
    """Estimate/GT associations within the time tolerance."""

    est_idx: np.ndarray  # indices into the estimate trajectory
    gt_idx: np.ndarray  # indices into the ground-truth trajectory
    time_errors: np.ndarray  # est_time - gt_time per pair (seconds)

    def __len__(self) -> int:
        return int(self.est_idx.shape[0])


def associate(
    est: Trajectory, gt: Trajectory, max_time_diff: float
) -> MatchedPairs:
    """Associate poses by timestamp with a bounded time difference.

    For every estimate timestamp we keep the *single nearest* GT timestamp
    and accept the pair iff ``|dt| <= max_time_diff`` (nearest neighbour
    against sorted GT via binary search). Each estimate is matched at most
    once; two close estimates may attach to the same GT sample. Ties
    between equidistant GT samples resolve to the earlier one
    deterministically. No interpolation: an unmatched sample is simply
    absent from the match set (and reported through the coverage), never
    zero-filled.
    """
    if max_time_diff < 0.0:
        raise InvalidRequestError("max_time_diff must be non-negative")

    insertion = np.searchsorted(gt.times, est.times)
    gt_idx_list: list[int] = []
    est_idx_list: list[int] = []
    dt_list: list[float] = []

    for ei, pos in enumerate(insertion):
        candidates = []
        if pos < len(gt):
            candidates.append(int(pos))
        if pos > 0:
            candidates.append(int(pos) - 1)
        best_j = min(
            candidates,
            key=lambda j: (
                abs(float(est.times[ei] - gt.times[j])),
                j,  # earlier GT wins exact-distance ties
            ),
        )
        dt = float(est.times[ei] - gt.times[best_j])
        if abs(dt) <= max_time_diff + 1e-12:
            est_idx_list.append(ei)
            gt_idx_list.append(best_j)
            dt_list.append(dt)

    if not est_idx_list:
        raise ZeroMatchesError(
            "no estimate/ground-truth pair within "
            f"max_time_diff={max_time_diff}s"
        )

    return MatchedPairs(
        est_idx=np.asarray(est_idx_list, dtype=np.int64),
        gt_idx=np.asarray(gt_idx_list, dtype=np.int64),
        time_errors=np.asarray(dt_list, dtype=np.float64),
    )
