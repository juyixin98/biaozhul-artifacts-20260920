"""Core 2D laser scan deskewing (motion distortion correction).

Frames and transform conventions
--------------------------------
- ``W``: world/odom frame, fixed for the duration of one scan.
- ``B(t)``: body (base) frame at time ``t``. The pose sequence gives
  ``T_W_B(t) = (x, y, theta)``: the pose of the body frame expressed in the
  world frame. Applying it maps body-frame coordinates to world coordinates.
- ``L``: laser frame, rigidly mounted on the body. The extrinsic
  ``T_B_L = (ex, ey, etheta)`` is the constant pose of the laser frame in
  the body frame (maps laser-frame coordinates to body-frame coordinates).

A point measured at time ``t_i`` with polar coordinates ``(range, angle)``
in the laser frame has world coordinates::

    p_W = T_W_B(t_i) @ T_B_L @ p_L

Deskewing re-expresses every point in the laser frame at the reference
time ``t_ref``::

    p_L_ref = inv(T_W_B(t_ref) @ T_B_L) @ T_W_B(t_i) @ T_B_L @ p_L

Poses are interpolated linearly in ``(x, y)`` and with spherical linear
interpolation (SciPy ``Slerp``) for the yaw angle, which is exact for the
constant-velocity motion used in the synthetic tests.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

# Tolerance (seconds) when checking that the pose sequence covers the
# scan time range and the reference time.
COVERAGE_TOL = 1e-9


class PoseCoverageError(ValueError):
    """Raised when the pose sequence does not cover a required time."""


class PoseSequenceError(ValueError):
    """Raised when the pose sequence itself is malformed."""


def _validate_pose_sequence(
    pose_times: np.ndarray, poses: np.ndarray
) -> None:
    if pose_times.ndim != 1 or poses.shape != (pose_times.size, 3):
        raise PoseSequenceError(
            "poses must have shape (N, 3) matching pose_times (N,)"
        )
    if pose_times.size < 2:
        raise PoseSequenceError(
            "at least 2 poses are required to interpolate over a scan"
        )
    dt = np.diff(pose_times)
    if not np.all(dt > 0):
        raise PoseSequenceError("pose_times must be strictly increasing")


def _check_coverage(t_min: float, t_max: float, pose_times: np.ndarray) -> None:
    lo, hi = pose_times[0], pose_times[-1]
    if t_min < lo - COVERAGE_TOL or t_max > hi + COVERAGE_TOL:
        raise PoseCoverageError(
            f"pose sequence covers [{lo:.6f}, {hi:.6f}] s but poses are "
            f"required over [{t_min:.6f}, {t_max:.6f}] s; refusing to "
            f"extrapolate"
        )


def interpolate_poses(
    pose_times: np.ndarray, poses: np.ndarray, query_times: np.ndarray
) -> np.ndarray:
    """Interpolate SE(2) poses at ``query_times``.

    ``poses`` is ``(N, 3)`` with columns ``(x, y, theta)`` giving ``T_W_B``.
    Translation is interpolated linearly; yaw is interpolated with Slerp so
    the +/-pi wrap is handled correctly. No extrapolation is performed.
    """
    pose_times = np.asarray(pose_times, dtype=float)
    poses = np.asarray(poses, dtype=float)
    query_times = np.asarray(query_times, dtype=float)
    _validate_pose_sequence(pose_times, poses)
    _check_coverage(float(query_times.min()), float(query_times.max()), pose_times)

    x = np.interp(query_times, pose_times, poses[:, 0])
    y = np.interp(query_times, pose_times, poses[:, 1])
    key_rots = Rotation.from_euler("z", poses[:, 2:3])
    slerp = Slerp(pose_times, key_rots)
    theta = slerp(query_times).as_rotvec()[:, 2]
    return np.column_stack([x, y, theta])


def se2_matrices(poses: np.ndarray) -> np.ndarray:
    """Build ``(N, 3, 3)`` homogeneous SE(2) matrices from ``(N, 3)`` poses."""
    poses = np.atleast_2d(np.asarray(poses, dtype=float))
    c, s = np.cos(poses[:, 2]), np.sin(poses[:, 2])
    n = poses.shape[0]
    mats = np.zeros((n, 3, 3))
    mats[:, 0, 0] = c
    mats[:, 0, 1] = -s
    mats[:, 1, 0] = s
    mats[:, 1, 1] = c
    mats[:, 0, 2] = poses[:, 0]
    mats[:, 1, 2] = poses[:, 1]
    mats[:, 2, 2] = 1.0
    return mats


def se2_inverse(mats: np.ndarray) -> np.ndarray:
    """Invert ``(N, 3, 3)`` SE(2) matrices analytically."""
    mats = np.asarray(mats, dtype=float)
    if mats.shape == (3, 3):
        mats = mats.reshape(1, 3, 3)
    inv = np.zeros_like(mats)
    R = mats[:, :2, :2]
    t = mats[:, :2, 2]
    Rt = np.swapaxes(R, 1, 2)
    inv[:, :2, :2] = Rt
    inv[:, :2, 2] = -np.einsum("nij,nj->ni", Rt, t)
    inv[:, 2, 2] = 1.0
    return inv


def deskew_scan(
    ranges: np.ndarray,
    angles: np.ndarray,
    point_times: np.ndarray,
    pose_times: np.ndarray,
    poses: np.ndarray,
    reference_time: float,
    extrinsic: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> np.ndarray:
    """Deskew one scan into the laser frame at ``reference_time``.

    Parameters
    ----------
    ranges, angles : (M,) arrays
        Polar coordinates of each point in the laser frame at its own
        sampling time.
    point_times : (M,) array
        Sampling time of each point (seconds, same clock as poses).
    pose_times : (K,) array, poses : (K, 3) array
        Body pose sequence ``T_W_B`` (x, y, theta in the world frame).
        Must strictly increase and cover ``[point_times.min(),
        point_times.max()]`` as well as ``reference_time``.
    reference_time : float
        Time to which all points are unified.
    extrinsic : (ex, ey, etheta)
        Constant extrinsic ``T_B_L``: pose of the laser frame in the body
        frame.

    Returns
    -------
    (M, 2) array
        Deskewed Cartesian points in the laser frame at ``reference_time``.

    Raises
    ------
    PoseCoverageError
        If the pose sequence does not cover every required time.
    PoseSequenceError
        If the pose sequence is malformed.
    """
    ranges = np.asarray(ranges, dtype=float)
    angles = np.asarray(angles, dtype=float)
    point_times = np.asarray(point_times, dtype=float)
    if not (ranges.shape == angles.shape == point_times.shape):
        raise ValueError("ranges, angles and point_times must have equal shape")
    if ranges.size == 0:
        raise ValueError("scan must contain at least one point")

    pose_times = np.asarray(pose_times, dtype=float)
    poses = np.asarray(poses, dtype=float)
    _validate_pose_sequence(pose_times, poses)
    _check_coverage(
        min(float(point_times.min()), float(reference_time)),
        max(float(point_times.max()), float(reference_time)),
        pose_times,
    )

    # Extrinsic T_B_L (laser pose in body frame), constant over the scan.
    T_B_L = se2_matrices(np.asarray(extrinsic, dtype=float).reshape(1, 3))[0]

    # Pose of each point's sampling time and of the reference time.
    query = np.concatenate([point_times, [reference_time]])
    interp = interpolate_poses(pose_times, poses, query)
    T_W_B_points = se2_matrices(interp[:-1])  # (M, 3, 3)
    T_W_B_ref = se2_matrices(interp[-1:])[0]  # (3, 3)

    # Laser pose in the world at each sampling time: T_W_L = T_W_B @ T_B_L.
    T_W_L = T_W_B_points @ T_B_L
    T_W_L_ref = T_W_B_ref @ T_B_L

    # Points to world, then into the reference laser frame.
    p_L = np.column_stack(
        [ranges * np.cos(angles), ranges * np.sin(angles), np.ones_like(ranges)]
    )
    p_W = np.einsum("nij,nj->ni", T_W_L, p_L)
    T_ref_inv = se2_inverse(T_W_L_ref.reshape(1, 3, 3))[0]
    p_L_ref = (T_ref_inv @ p_W.T).T
    return p_L_ref[:, :2]
