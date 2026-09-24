"""SE(2) geometry: poses are (x, y, theta); theta in radians.

A pose represents the rigid transform ``T = [R(theta)  t]`` acting on points
expressed in the body frame.  Composition is the usual matrix product of the
3x3 homogeneous transforms; we keep only the minimal 3-vector form.
"""

from __future__ import annotations

import numpy as np

TWO_PI = 2.0 * np.pi


def wrap_angle(angle: float | np.ndarray) -> float | np.ndarray:
    """Wrap an angle (or array of angles) to [-pi, pi)."""
    return (np.asarray(angle) + np.pi) % TWO_PI - np.pi


def rotation2(theta: float | np.ndarray) -> np.ndarray:
    """2x2 rotation matrix for ``theta``."""
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s], [s, c]])


def compose(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Compose two SE(2) poses: return ``a oplus b`` (T_a @ T_b)."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    c, s = np.cos(a[2]), np.sin(a[2])
    x = a[0] + c * b[0] - s * b[1]
    y = a[1] + s * b[0] + c * b[1]
    theta = wrap_angle(a[2] + b[2])
    return np.array([x, y, theta])


def inverse(pose: np.ndarray) -> np.ndarray:
    """Return the inverse SE(2) pose T^{-1} = (R^T, -R^T t)."""
    pose = np.asarray(pose, dtype=float)
    c, s = np.cos(pose[2]), np.sin(pose[2])
    x = -c * pose[0] - s * pose[1]
    y = s * pose[0] - c * pose[1]
    theta = wrap_angle(-pose[2])
    return np.array([x, y, theta])


def between(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Relative pose ``a^{-1} oplus b`` (the measurement from node a to b)."""
    return compose(inverse(a), b)


def normalize_pose(pose: np.ndarray) -> np.ndarray:
    """Return a pose copy with its angle wrapped to (-pi, pi]."""
    pose = np.array(pose, dtype=float)
    pose[2] = wrap_angle(pose[2])
    return pose
