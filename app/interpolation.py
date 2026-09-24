"""Time interpolation of body pose trajectories.

Poses are interpolated component-wise: linear for (x, y), and linear on the
unwrapped heading for theta (constant-turn-rate model). Interpolation is
only defined inside the time span covered by the pose samples; requests
outside that span raise :class:`PoseCoverageError` instead of silently
extrapolating.
"""

from __future__ import annotations

import numpy as np


class PoseCoverageError(ValueError):
    """Raised when a query time is not covered by the pose trajectory."""


def interpolate_pose(
    pose_times: np.ndarray,
    pose_xytheta: np.ndarray,
    query_times: np.ndarray,
) -> np.ndarray:
    """Interpolate poses at ``query_times``.

    Parameters
    ----------
    pose_times:
        (M,) strictly increasing sample times.
    pose_xytheta:
        (M, 3) array of (x, y, theta) samples.
    query_times:
        (N,) query times. Every query must satisfy
        ``pose_times[0] <= t <= pose_times[-1]``.

    Returns
    -------
    (N, 3) array of interpolated (x, y, theta).

    Raises
    ------
    PoseCoverageError
        If any query time lies outside the covered span, or fewer than two
        pose samples are given.
    """
    pose_times = np.asarray(pose_times, dtype=float)
    pose_xytheta = np.asarray(pose_xytheta, dtype=float)
    query_times = np.asarray(query_times, dtype=float)

    if pose_times.ndim != 1 or pose_times.size < 2:
        raise PoseCoverageError(
            "pose trajectory needs at least 2 samples to interpolate"
        )
    if np.any(np.diff(pose_times) <= 0):
        raise ValueError("pose times must be strictly increasing")

    t0, t1 = pose_times[0], pose_times[-1]
    outside = (query_times < t0) | (query_times > t1)
    if np.any(outside):
        bad = query_times[outside]
        raise PoseCoverageError(
            f"{outside.sum()} query time(s) outside pose coverage "
            f"[{t0:.6f}, {t1:.6f}]; first offending t={bad[0]:.6f}. "
            "Extrapolation is not supported."
        )

    theta_unwrapped = np.unwrap(pose_xytheta[:, 2])
    x = np.interp(query_times, pose_times, pose_xytheta[:, 0])
    y = np.interp(query_times, pose_times, pose_xytheta[:, 1])
    theta = np.interp(query_times, pose_times, theta_unwrapped)
    return np.column_stack([x, y, theta])
