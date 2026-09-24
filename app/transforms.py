"""Planar SE(2) rigid-body transforms.

Conventions
-----------
A pose ``(x, y, theta)`` denotes the transform ``T_world_body``: it maps
points expressed in the body frame into the world frame::

    p_world = R(theta) @ p_body + [x, y]

All transforms are represented as 3x3 homogeneous matrices. Composition
``A @ B`` applies ``B`` first (rightmost), matching standard column-vector
convention.
"""

from __future__ import annotations

import numpy as np


def se2(x: float, y: float, theta: float) -> np.ndarray:
    """Build the 3x3 homogeneous matrix for pose (x, y, theta)."""
    c, s = np.cos(theta), np.sin(theta)
    return np.array(
        [
            [c, -s, x],
            [s, c, y],
            [0.0, 0.0, 1.0],
        ]
    )


def se2_inv(t: np.ndarray) -> np.ndarray:
    """Analytic inverse of an SE(2) homogeneous matrix."""
    r = t[:2, :2]
    p = t[:2, 2]
    inv = np.eye(3)
    inv[:2, :2] = r.T
    inv[:2, 2] = -r.T @ p
    return inv


def apply(t: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Apply an SE(2) matrix to an (N, 2) array of points."""
    points = np.asarray(points, dtype=float)
    return points @ t[:2, :2].T + t[:2, 2]
