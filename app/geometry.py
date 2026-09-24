"""SE(3) / rotation geometry helpers (NumPy only).

Poses follow the TUM convention:
  position    = (x, y, z)
  quaternion  = (qx, qy, qz, qw), Hamilton convention

Rotational distance is the *geodesic angle* of the relative rotation
``angle(R1^-1 R2)``.  Because q and -q represent the same rotation, both
representations are canonicalised to the hemisphere qw >= 0 first, so a
trajectory whose quaternions jump sign across the -1/1 boundary is measured
correctly (no spurious ~360 degree errors).
"""

from __future__ import annotations

import numpy as np

from .errors import EvaluationError

_EPS = 1e-12


def as_vec3(v: Any, what: str) -> np.ndarray:
    """Validate a 3-vector of finite floats and return it as float64 array."""
    arr = np.asarray(v, dtype=np.float64)
    if arr.shape != (3,):
        raise EvaluationError(
            "INVALID_POSE", f"{what} must be a 3-element array.", {"got_shape": list(arr.shape)}
        )
    if not np.all(np.isfinite(arr)):
        raise EvaluationError("INVALID_POSE", f"{what} contains non-finite values.")
    return arr


def normalize_quat(q: np.ndarray) -> np.ndarray:
    """Normalize a (qx, qy, qz, qw) quaternion; reject zero/invalid quats."""
    n = float(np.linalg.norm(q))
    if n < _EPS or not np.isfinite(n):
        raise EvaluationError(
            "INVALID_POSE",
            "Quaternion has (near-)zero norm and does not represent a rotation.",
            {"norm": n},
        )
    qn = q / n
    # q and -q are the same rotation: fix the sign so geometry is unambiguous.
    if qn[3] < 0.0:
        qn = -qn
    return qn


def quat_to_rot(q: np.ndarray) -> np.ndarray:
    """Convert a Hamilton (qx, qy, qz, qw) quaternion to a 3x3 rotation."""
    qx, qy, qz, qw = q
    return np.array(
        [
            [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
            [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
            [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)],
        ],
        dtype=np.float64,
    )


def rot_to_quat(R: np.ndarray) -> np.ndarray:
    """Convert a 3x3 rotation matrix to canonical (qx, qy, qz, qw).

    Uses the numerically stable branch-selection form of Shepperd's method.
    """
    t = np.trace(R)
    if t > 0.0:
        s = np.sqrt(t + 1.0) * 2.0
        qw = 0.25 * s
        qx = (R[2, 1] - R[1, 2]) / s
        qy = (R[0, 2] - R[2, 0]) / s
        qz = (R[1, 0] - R[0, 1]) / s
    else:  # branch on the largest diagonal term
        i = int(np.argmax(np.diag(R)))
        if i == 0:
            s = np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2.0
            qw = (R[2, 1] - R[1, 2]) / s
            qx = 0.25 * s
            qy = (R[0, 1] + R[1, 0]) / s
            qz = (R[0, 2] + R[2, 0]) / s
        elif i == 1:
            s = np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2.0
            qw = (R[0, 2] - R[2, 0]) / s
            qx = (R[0, 1] + R[1, 0]) / s
            qy = 0.25 * s
            qz = (R[1, 2] + R[2, 1]) / s
        else:
            s = np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2.0
            qw = (R[1, 0] - R[0, 1]) / s
            qx = (R[0, 2] + R[2, 0]) / s
            qy = (R[1, 2] + R[2, 1]) / s
            qz = 0.25 * s
    q = np.array([qx, qy, qz, qw], dtype=np.float64)
    return normalize_quat(q)


def rotation_angle(R: np.ndarray) -> float:
    """Geodesic rotation angle of SO(3) matrix R in radians, in [0, pi]."""
    c = (np.trace(R) - 1.0) / 2.0
    # Numerical guard for arccos.
    return float(np.arccos(np.clip(c, -1.0, 1.0)))


def rotation_distance_rad(R1: np.ndarray, R2: np.ndarray) -> float:
    """Angular distance between two rotations: angle(R1^T R2), in [0, pi]."""
    return rotation_angle(R1.T @ R2)


def invert_pose(R: np.ndarray, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Invert an SE(3) pose with rotation R and translation t."""
    return R.T, -(R.T @ t)


def relative_pose(
    R_a: np.ndarray, t_a: np.ndarray, R_b: np.ndarray, t_b: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Relative pose from a to b, i.e. T_a^{-1} T_b."""
    Ri, ti = invert_pose(R_a, t_a)
    return Ri @ R_b, Ri @ (t_b - t_a)
