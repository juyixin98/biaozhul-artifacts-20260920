"""ATE and fixed-time-span RPE metrics.

All errors are computed *after* the requested alignment (rigid or
similarity). Nothing is zero-filled: a frame without a match, or an RPE
interval that cannot be formed because data is missing, is simply absent
from the statistics (and counted, so coverage is honest).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .alignment import AlignmentResult
from .errors import InvalidRequestError
from .rotation import angular_distance, rot_chain
from .trajectory import MatchedPairs, Trajectory


def _stats(values: np.ndarray) -> dict:
    """RMSE/mean/median/std/max for a 1-D error array (never empty)."""
    v = np.asarray(values, dtype=np.float64)
    return {
        "rmse": float(np.sqrt(np.mean(v ** 2))),
        "mean": float(np.mean(v)),
        "median": float(np.median(v)),
        "std": float(np.std(v)),
        "max": float(np.max(v)),
    }


@dataclass
class AlignedSequence:
    """Matched samples expressed in the ground-truth frame."""

    times_est: np.ndarray
    times_gt: np.ndarray
    time_errors: np.ndarray
    positions: np.ndarray  # aligned estimate positions
    rotations: np.ndarray  # aligned estimate rotations
    gt_positions: np.ndarray
    gt_rotations: np.ndarray

    def __len__(self) -> int:
        return int(self.positions.shape[0])


def make_aligned_sequence(
    est: Trajectory, gt: Trajectory, pairs: MatchedPairs, a: AlignmentResult
) -> AlignedSequence:
    """Apply ``p -> s R p + t`` and ``Q -> R Q`` to the matched estimates."""
    ep = est.positions[pairs.est_idx]
    er = est.rotations[pairs.est_idx]
    positions = a.scale * (ep @ a.rotation.T) + a.translation
    # Aligned world-from-body rotation: R @ R_est.
    rotations = np.einsum("ij,njk->nik", a.rotation, er)
    return AlignedSequence(
        times_est=est.times[pairs.est_idx],
        times_gt=gt.times[pairs.gt_idx],
        time_errors=pairs.time_errors,
        positions=positions,
        rotations=rotations,
        gt_positions=gt.positions[pairs.gt_idx],
        gt_rotations=gt.rotations[pairs.gt_idx],
    )


def compute_ate(seq: AlignedSequence) -> dict:
    """Absolute trajectory error over matched frames.

    Translation error is the Euclidean position distance (metres). Rotation
    error is the SO(3) geodesic angle (radians; degrees included for
    convenience). Both are per-frame, plus aggregate statistics.
    """
    trans_err = np.linalg.norm(seq.positions - seq.gt_positions, axis=1)
    rot_err = np.array(
        [
            angular_distance(seq.rotations[i], seq.gt_rotations[i])
            for i in range(len(seq))
        ]
    )
    frames = []
    for i in range(len(seq)):
        frames.append(
            {
                "timestamp_est": float(seq.times_est[i]),
                "timestamp_gt": float(seq.times_gt[i]),
                "time_error": float(seq.time_errors[i]),
                "trans_error": float(trans_err[i]),
                "rot_error_rad": float(rot_err[i]),
                "rot_error_deg": float(np.degrees(rot_err[i])),
            }
        )
    return {
        "num_frames": int(len(seq)),
        "trans": {**_stats(trans_err), "unit": "meter"},
        "rot_rad": {**_stats(rot_err), "unit": "radian"},
        "rot_deg": {**_stats(np.degrees(rot_err)), "unit": "degree"},
        "frames": frames,
    }


def compute_rpe(
    seq: AlignedSequence, delta: float, tolerance: float
) -> dict:
    """Relative pose error for pairs separated by ``delta`` seconds.

    For every matched frame i we pick the later frame j whose time gap is
    closest to ``delta``, accepting it iff ``|gap - delta| <= tolerance``.
    Missing samples therefore just yield fewer pairs (``num_pairs`` reports
    how many); gaps are never fabricated.

    Relative motion (frame i -> j, expressed in frame i):
        p_rel = R_i^T (p_j - p_i),   R_rel = R_i^T R_j
    Errors:
        e_t = ||p_rel_est - p_rel_gt||
        e_R = angle(R_rel_est, R_rel_gt)   (geodesic, wrapping-free)
    """
    if delta <= 0.0:
        raise InvalidRequestError("rpe delta must be strictly positive")
    if tolerance < 0.0:
        raise InvalidRequestError("rpe tolerance must be non-negative")

    times = seq.times_est
    order = np.argsort(times, kind="stable")
    times = times[order]
    n = len(times)

    intervals = []
    used = set()
    for pos_i in range(n - 1):
        oi = int(order[pos_i])
        target = times[pos_i] + delta
        # Search only later samples.
        k = int(np.searchsorted(times, target, side="left"))
        candidates = []
        for c in (k - 1, k, k + 1):
            if pos_i < c < n:
                candidates.append(c)
        if not candidates:
            continue
        pos_j = min(
            candidates,
            key=lambda c: (abs(times[c] - target), c),
        )
        gap = float(times[pos_j] - times[pos_i])
        if abs(gap - delta) > tolerance + 1e-12:
            continue
        oj = int(order[pos_j])
        if oj in used:  # each later frame closes at most one interval
            continue
        used.add(oj)
        intervals.append((oi, oj, gap))

    if not intervals:
        return {
            "delta": float(delta),
            "tolerance": float(tolerance),
            "num_pairs": 0,
            "trans": None,
            "rot_rad": None,
            "rot_deg": None,
            "pairs": [],
            "note": "no matched frame pairs within the span tolerance",
        }

    trans_err = []
    rot_err = []
    pairs_out = []
    for oi, oj, gap in intervals:
        p_rel_gt = seq.gt_rotations[oi].T @ (
            seq.gt_positions[oj] - seq.gt_positions[oi]
        )
        r_rel_gt = rot_chain(seq.gt_rotations[oi], seq.gt_rotations[oj])
        p_rel_est = seq.rotations[oi].T @ (
            seq.positions[oj] - seq.positions[oi]
        )
        r_rel_est = rot_chain(seq.rotations[oi], seq.rotations[oj])
        et = float(np.linalg.norm(p_rel_est - p_rel_gt))
        er = angular_distance(r_rel_est, r_rel_gt)
        trans_err.append(et)
        rot_err.append(er)
        pairs_out.append(
            {
                "t0": float(seq.times_est[oi]),
                "t1": float(seq.times_est[oj]),
                "actual_span": float(gap),
                "trans_error": et,
                "rot_error_rad": float(er),
                "rot_error_deg": float(np.degrees(er)),
            }
        )

    trans_arr = np.asarray(trans_err)
    rot_arr = np.asarray(rot_err)
    return {
        "delta": float(delta),
        "tolerance": float(tolerance),
        "num_pairs": len(intervals),
        "trans": {**_stats(trans_arr), "unit": "meter"},
        "rot_rad": {**_stats(rot_arr), "unit": "radian"},
        "rot_deg": {**_stats(np.degrees(rot_arr)), "unit": "degree"},
        "pairs": pairs_out,
    }
