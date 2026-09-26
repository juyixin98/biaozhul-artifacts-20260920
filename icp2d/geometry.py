"""2D rigid transform (SE(2)) helpers.

Pose convention: pose = (tx, ty, theta) maps points from the source (scan)
frame into the target (map) frame:  q = R(theta) @ p + t.
"""

from __future__ import annotations

import numpy as np

ArrayLike = np.ndarray


def rotation_matrix(theta: float) -> np.ndarray:
    """Return the 2x2 rotation matrix for angle ``theta`` (radians)."""
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s], [s, c]])


def transform_points(points: np.ndarray, pose: np.ndarray) -> np.ndarray:
    """Apply pose (tx, ty, theta) to an (N, 2) point array."""
    points = np.asarray(points, dtype=float)
    tx, ty, theta = pose
    return points @ rotation_matrix(theta).T + np.array([tx, ty])


def compose_poses(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Compose two poses: result applies ``b`` first, then ``a``."""
    ra = rotation_matrix(a[2])
    t = ra @ b[:2] + a[:2]
    theta = normalize_angle(a[2] + b[2])
    return np.array([t[0], t[1], theta])


def inverse_pose(pose: np.ndarray) -> np.ndarray:
    """Return the inverse of pose (tx, ty, theta)."""
    r = rotation_matrix(pose[2])
    t = -r.T @ pose[:2]
    return np.array([t[0], t[1], normalize_angle(-pose[2])])


def normalize_angle(theta: float) -> float:
    """Wrap angle to [-pi, pi)."""
    return float((theta + np.pi) % (2.0 * np.pi) - np.pi)


def pose_error(estimated: np.ndarray, ground_truth: np.ndarray) -> np.ndarray:
    """Element-wise absolute pose error; the angular part is wrapped."""
    estimated = np.asarray(estimated, dtype=float)
    ground_truth = np.asarray(ground_truth, dtype=float)
    return np.array(
        [
            abs(estimated[0] - ground_truth[0]),
            abs(estimated[1] - ground_truth[1]),
            abs(normalize_angle(estimated[2] - ground_truth[2])),
        ]
    )
