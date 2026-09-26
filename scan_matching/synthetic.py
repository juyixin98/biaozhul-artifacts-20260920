"""Synthetic scene and scan generation for offline testing.

No hardware, no ROS: scenes are dense point clouds sampled from simple
wall geometries, and a "scan" is the scene observed from a sensor pose
with range/field-of-view clipping and Gaussian noise.
"""

from __future__ import annotations

import numpy as np

from scan_matching.geometry import SE2Pose, apply_transform, inverse


def _sample_segment(p0: tuple[float, float],
                    p1: tuple[float, float],
                    spacing: float) -> np.ndarray:
    """Densely sample points along a wall segment."""
    p0 = np.asarray(p0, dtype=float)
    p1 = np.asarray(p1, dtype=float)
    length = float(np.linalg.norm(p1 - p0))
    count = max(2, int(length / spacing) + 1)
    t = np.linspace(0.0, 1.0, count)[:, None]
    return p0[None, :] * (1.0 - t) + p1[None, :] * t


def generate_scene(shape: str = "room", spacing: float = 0.05) -> np.ndarray:
    """Generate a synthetic 2D scene as an (N, 2) point cloud.

    Shapes:
        "room":  10 m x 8 m rectangular room with a pillar, well
                 constrained in all pose directions.
        "line":  a single straight wall (degenerate for ICP: translation
                 along the wall is unobservable).
        "corridor": two parallel walls (degenerate along the corridor).
    """
    if shape == "room":
        walls = [
            ((-5.0, -4.0), (5.0, -4.0)),
            ((5.0, -4.0), (5.0, 4.0)),
            ((5.0, 4.0), (-5.0, 4.0)),
            ((-5.0, 4.0), (-5.0, -4.0)),
            # Interior pillar so the scene is fully constrained.
            ((1.0, 1.0), (2.0, 1.0)),
            ((2.0, 1.0), (2.0, 2.0)),
            ((2.0, 2.0), (1.0, 2.0)),
            ((1.0, 2.0), (1.0, 1.0)),
        ]
    elif shape == "line":
        walls = [((-6.0, 0.0), (6.0, 0.0))]
    elif shape == "corridor":
        walls = [
            ((-8.0, -1.5), (8.0, -1.5)),
            ((-8.0, 1.5), (8.0, 1.5)),
        ]
    else:
        raise ValueError(f"unknown scene shape: {shape!r}")
    return np.vstack([_sample_segment(a, b, spacing) for a, b in walls])


def generate_scan(scene: np.ndarray,
                  sensor_pose: SE2Pose,
                  max_range: float = 8.0,
                  fov: float = 2.0 * np.pi,
                  noise_sigma: float = 0.01,
                  rng: np.random.Generator | None = None) -> np.ndarray:
    """Simulate a 2D laser scan of ``scene`` from ``sensor_pose``.

    Scene points are transformed into the sensor frame and clipped by
    range and field of view, then Gaussian noise is added. Points from
    different sensor poses of the same scene therefore overlap only
    partially, mimicking real sequential scans.
    """
    if max_range <= 0.0:
        raise ValueError("max_range must be > 0")
    if not 0.0 < fov <= 2.0 * np.pi:
        raise ValueError("fov must be in (0, 2*pi]")
    rng = rng or np.random.default_rng()
    local = apply_transform(scene, inverse(sensor_pose))
    ranges = np.linalg.norm(local, axis=1)
    angles = np.arctan2(local[:, 1], local[:, 0])
    half_fov = fov / 2.0
    visible = (ranges <= max_range) & (ranges > 1e-6) & (np.abs(angles) <= half_fov)
    scan = local[visible]
    if noise_sigma > 0.0 and len(scan) > 0:
        scan = scan + rng.normal(0.0, noise_sigma, size=scan.shape)
    return scan


def add_outliers(points: np.ndarray,
                 count: int,
                 rng: np.random.Generator,
                 center: tuple[float, float] = (0.0, 0.0),
                 spread: float = 5.0) -> np.ndarray:
    """Return a new cloud with ``count`` uniformly random outlier points."""
    outliers = rng.uniform(-spread, spread, size=(count, 2)) + np.asarray(center)
    return np.vstack([points, outliers])


def partial_overlap_scan(scene: np.ndarray,
                         sensor_pose: SE2Pose,
                         keep_fraction: float = 0.6,
                         rng: np.random.Generator | None = None,
                         **scan_kwargs) -> np.ndarray:
    """Scan with an additional random subsample, reducing overlap further."""
    if not 0.0 < keep_fraction <= 1.0:
        raise ValueError("keep_fraction must be in (0, 1]")
    rng = rng or np.random.default_rng()
    scan = generate_scan(scene, sensor_pose, rng=rng, **scan_kwargs)
    keep = rng.random(len(scan)) < keep_fraction
    return scan[keep]
