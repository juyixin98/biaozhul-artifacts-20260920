"""SE(2) rigid-body pose algebra.

A pose is a length-3 vector ``(x, y, theta)`` representing a 2D frame
with translation ``(x, y)`` and rotation ``theta`` (radians, CCW).
All functions accept and return ``numpy.ndarray`` of shape ``(3,)``.
"""

from __future__ import annotations

import numpy as np

TWO_PI = 2.0 * np.pi


def wrap_angle(angle: float | np.ndarray) -> float | np.ndarray:
    """Normalize an angle (or array of angles) to ``[-pi, pi)``."""
    return (angle + np.pi) % TWO_PI - np.pi


def rotation(theta: float) -> np.ndarray:
    """2D rotation matrix for angle ``theta``."""
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s], [s, c]])


def compose(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Pose composition ``a (+) b``: apply ``b`` in the frame of ``a``."""
    c, s = np.cos(a[2]), np.sin(a[2])
    return np.array(
        [
            a[0] + c * b[0] - s * b[1],
            a[1] + s * b[0] + c * b[1],
            wrap_angle(a[2] + b[2]),
        ]
    )


def inverse(a: np.ndarray) -> np.ndarray:
    """Inverse pose of ``a`` such that ``compose(a, inverse(a)) == 0``."""
    c, s = np.cos(a[2]), np.sin(a[2])
    return np.array(
        [
            -c * a[0] - s * a[1],
            s * a[0] - c * a[1],
            wrap_angle(-a[2]),
        ]
    )


def between(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Relative pose that takes frame ``a`` to frame ``b``: ``a^-1 (+) b``."""
    return compose(inverse(a), b)
