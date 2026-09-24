"""Rotation utilities: quaternions, rotation matrices and angular distance.

Quaternion convention: ``[w, x, y, z]`` (scalar-first), Hamilton.
Angular distance is the geodesic angle on SO(3), i.e.
``arccos((trace(R1^T R2) - 1) / 2)`` in [0, pi], which is the invariant,
unwrapping-free way to measure rotation error (no branch cut at +/-pi).
"""

from __future__ import annotations

import numpy as np

from .errors import InvalidOrientationError

_FLOAT = np.float64
_EPS = 1e-12


def quat_to_matrix(q: np.ndarray) -> np.ndarray:
    """Normalize a quaternion and return the 3x3 rotation matrix.

    A zero-norm (or near-zero-norm) quaternion is an invalid orientation and
    raises ``InvalidOrientationError`` -- we never substitute an identity.
    """
    q = np.asarray(q, dtype=_FLOAT).reshape(4)
    norm = float(np.linalg.norm(q))
    if norm < _EPS:
        raise InvalidOrientationError(
            "zero-norm quaternion is not a valid orientation"
        )
    w, x, y, z = q / norm
    # Canonicalize the double cover (q and -q describe the same rotation).
    if w < 0.0:
        w, x, y, z = -w, -x, -y, -z
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=_FLOAT,
    )


def matrix_to_quat(R: np.ndarray) -> np.ndarray:
    """Convert a 3x3 rotation matrix to a normalized ``[w, x, y, z]`` quaternion.

    Assumes ``R`` is (numerically) orthogonal; the result is normalized and
    canonicalized to ``w >= 0``.
    """
    R = np.asarray(R, dtype=_FLOAT)
    trace = R[0, 0] + R[1, 1] + R[2, 2]
    if trace > 0.0:
        s = 2.0 * np.sqrt(trace + 1.0)
        q = np.array(
            [
                0.25 * s,
                (R[2, 1] - R[1, 2]) / s,
                (R[0, 2] - R[2, 0]) / s,
                (R[1, 0] - R[0, 1]) / s,
            ]
        )
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        q = np.array(
            [
                (R[2, 1] - R[1, 2]) / s,
                0.25 * s,
                (R[0, 1] + R[1, 0]) / s,
                (R[0, 2] + R[2, 0]) / s,
            ]
        )
    elif R[1, 1] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        q = np.array(
            [
                (R[0, 2] - R[2, 0]) / s,
                (R[0, 1] + R[1, 0]) / s,
                0.25 * s,
                (R[1, 2] + R[2, 1]) / s,
            ]
        )
    else:
        s = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
        q = np.array(
            [
                (R[1, 0] - R[0, 1]) / s,
                (R[0, 2] + R[2, 0]) / s,
                (R[1, 2] + R[2, 1]) / s,
                0.25 * s,
            ]
        )
    q /= np.linalg.norm(q)
    if q[0] < 0.0:
        q = -q
    return q


def angular_distance(R1: np.ndarray, R2: np.ndarray) -> float:
    """Geodesic angle (radians) between two rotations, in ``[0, pi]``.

    ``angle = arccos(clip((trace(R1^T R2) - 1) / 2, -1, 1))``.
    This is the natural metric on SO(3): continuous through pi and free of
    Euler-angle singularities / wrapping artefacts, so rotations crossing the
    +/-pi or 2pi boundary are scored correctly.
    """
    R1 = np.asarray(R1, dtype=_FLOAT)
    R2 = np.asarray(R2, dtype=_FLOAT)
    cos = (np.trace(R1.T @ R2) - 1.0) / 2.0
    return float(np.arccos(np.clip(cos, -1.0, 1.0)))


def rot_chain(ra: np.ndarray, rb: np.ndarray) -> np.ndarray:
    """Relative rotation ``ra^T rb`` (frame ``a`` -> frame ``b`` in frame a)."""
    return np.asarray(ra, dtype=_FLOAT).T @ np.asarray(rb, dtype=_FLOAT)
