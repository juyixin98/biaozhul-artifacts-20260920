"""Shared helpers for building synthetic trajectories in tests."""

from __future__ import annotations

import numpy as np


def q_from_axis_angle(axis: np.ndarray, angle: float) -> np.ndarray:
    """Unit quaternion (qx, qy, qz, qw) for an axis-angle rotation."""
    axis = np.asarray(axis, dtype=np.float64)
    axis = axis / np.linalg.norm(axis)
    return np.concatenate([np.sin(angle / 2.0) * axis, [np.cos(angle / 2.0)]])


def q_identity() -> np.ndarray:
    return np.array([0.0, 0.0, 0.0, 1.0])


def pose(time: float, position, quaternion) -> dict:
    p = np.asarray(position, dtype=np.float64)
    if p.shape != (3,):
        p = np.broadcast_to(p, (3,)).copy()
    q = np.asarray(quaternion, dtype=np.float64)
    return {
        "time": float(time),
        "position": [float(v) for v in p],
        "quaternion_xyzw": [float(v) for v in q],
    }


def transform_pose(position, quaternion, R, t, s=1.0):
    """Apply pose frame transform p' = s R p + t, q' = R * q."""
    p = np.asarray(position, dtype=np.float64)
    q = np.asarray(quaternion, dtype=np.float64)
    # Rotate quaternion by R: convert R to quat and Hamilton-multiply.
    qR = _rot_to_quat(R)
    return s * R @ p + np.asarray(t, dtype=np.float64), _quat_multiply(qR, q)


def _quat_multiply(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array(
        [
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz,
        ]
    )


def _rot_to_quat(R: np.ndarray) -> np.ndarray:
    from app.geometry import rot_to_quat

    return rot_to_quat(R)
