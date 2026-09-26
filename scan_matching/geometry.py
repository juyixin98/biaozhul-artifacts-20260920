"""SE(2) pose helpers for 2D scan matching.

A pose is (x, y, theta): translation plus rotation angle in radians.
Points are stored as (N, 2) float arrays, one point per row.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SE2Pose:
    """Immutable 2D rigid-body pose."""

    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0

    def rotation_matrix(self) -> np.ndarray:
        c = math.cos(self.theta)
        s = math.sin(self.theta)
        return np.array([[c, -s], [s, c]], dtype=float)

    def as_matrix(self) -> np.ndarray:
        """Return the 3x3 homogeneous transform matrix."""
        m = np.eye(3)
        m[:2, :2] = self.rotation_matrix()
        m[:2, 2] = (self.x, self.y)
        return m

    @staticmethod
    def from_matrix(m: np.ndarray) -> "SE2Pose":
        return SE2Pose(
            x=float(m[0, 2]),
            y=float(m[1, 2]),
            theta=math.atan2(m[1, 0], m[0, 0]),
        )

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "theta": self.theta}

    @staticmethod
    def from_dict(d: dict) -> "SE2Pose":
        return SE2Pose(
            x=float(d.get("x", 0.0)),
            y=float(d.get("y", 0.0)),
            theta=float(d.get("theta", 0.0)),
        )


def apply_transform(points: np.ndarray, pose: SE2Pose) -> np.ndarray:
    """Apply an SE(2) pose to an (N, 2) point array, returning a new array."""
    points = np.asarray(points, dtype=float)
    return points @ pose.rotation_matrix().T + np.array([pose.x, pose.y])


def compose(a: SE2Pose, b: SE2Pose) -> SE2Pose:
    """Return the pose a ∘ b (apply b first, then a)."""
    return SE2Pose.from_matrix(a.as_matrix() @ b.as_matrix())


def inverse(pose: SE2Pose) -> SE2Pose:
    """Return the inverse SE(2) pose, in closed form: (Rᵀ, -Rᵀt)."""
    r_t = pose.rotation_matrix().T
    t = -(r_t @ np.array([pose.x, pose.y]))
    return SE2Pose(x=float(t[0]), y=float(t[1]), theta=-pose.theta)


def pose_error(estimated: SE2Pose, truth: SE2Pose) -> dict:
    """Absolute translation/rotation error between two poses."""
    return {
        "translation": math.hypot(estimated.x - truth.x, estimated.y - truth.y),
        "rotation": abs(_wrap_angle(estimated.theta - truth.theta)),
    }


def _wrap_angle(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi
