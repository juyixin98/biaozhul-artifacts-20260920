"""Pytest fixtures and trajectory builders shared by the test suite."""

import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.rotation import angular_distance, quat_to_matrix  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def qz(angle: float) -> list[float]:
    """Quaternion [w,x,y,z] for a rotation of ``angle`` rad about z."""
    return [np.cos(angle / 2), 0.0, 0.0, np.sin(angle / 2)]


def circle_poses(n=24, radius=2.0, omega=0.4, angle0=0.0, t0=0.0, dt=0.5,
                 pos_fn=None, rot_fn=None):
    """Poses moving around a z-axis circle, body yaw heading along motion.

    ``pos_fn(i, p)`` / ``rot_fn(i, q)`` can mutate the nominal pose.
    """
    poses = []
    for i in range(n):
        t = t0 + i * dt
        a = angle0 + i * omega
        x = radius * np.cos(a)
        y = radius * np.sin(a)
        pos = [x, y, 0.0]
        q = qz(a + np.pi / 2)  # heading tangent
        if pos_fn is not None:
            pos = pos_fn(i, np.array(pos, dtype=float))
        if rot_fn is not None:
            q = rot_fn(i, np.array(q, dtype=float))
        poses.append(
            {"timestamp": round(t, 9), "position": [float(v) for v in pos],
             "orientation": [float(v) for v in q]}
        )
    return poses


def transform_poses(poses, R=None, t=None, s=1.0):
    """Apply p' = s R p + t, Q' = R Q to raw pose dicts."""
    R = np.eye(3) if R is None else R
    t = np.zeros(3) if t is None else np.asarray(t)
    out = []
    for p in poses:
        pos = s * R @ np.array(p["position"]) + t
        quat = np.array(p["orientation"])
        Q = R @ quat_to_matrix(quat)
        from app.rotation import matrix_to_quat

        q = matrix_to_quat(Q)
        out.append(
            {"timestamp": p["timestamp"],
             "position": [float(v) for v in pos],
             "orientation": [float(v) for v in q]}
        )
    return out


def zrot_matrix(angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


__all__ = [
    "client", "qz", "circle_poses", "transform_poses", "zrot_matrix",
    "angular_distance", "quat_to_matrix",
]
