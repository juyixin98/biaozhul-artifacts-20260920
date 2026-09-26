"""Synthetic scan generation for offline testing and demos.

All data is synthetic: scans are sampled from simple geometric worlds
(rectangle room, straight wall) and transformed with a known ground-truth
pose, optionally corrupted with noise, outliers and partial overlap. No
hardware, no ROS, no visualization.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geometry import transform_points


@dataclass
class SyntheticScenario:
    """A ready-made matching problem with known ground truth."""

    source: np.ndarray  # (N, 2) scan in the sensor frame
    target: np.ndarray  # (M, 2) reference scan in the map frame
    ground_truth_pose: np.ndarray  # (tx, ty, theta) mapping source -> target
    description: str


def sample_polygon_scan(vertices: np.ndarray, spacing: float = 0.1) -> np.ndarray:
    """Sample points uniformly (by arc length) along a closed polygon."""
    vertices = np.asarray(vertices, dtype=float)
    points = []
    n = len(vertices)
    for i in range(n):
        a, b = vertices[i], vertices[(i + 1) % n]
        length = float(np.hypot(*(b - a)))
        count = max(2, int(np.ceil(length / spacing)) + 1)
        ts = np.linspace(0.0, 1.0, count, endpoint=False)
        points.append(a[None, :] + ts[:, None] * (b - a)[None, :])
    return np.vstack(points)


def rectangle_room_scan(
    width: float = 14.0, height: float = 9.0, spacing: float = 0.1
) -> np.ndarray:
    """Scan of a rectangular room centred at the origin."""
    w, h = width / 2.0, height / 2.0
    vertices = np.array([[-w, -h], [w, -h], [w, h], [-w, h]])
    return sample_polygon_scan(vertices, spacing)


def straight_wall_scan(
    length: float = 10.0, spacing: float = 0.1, center: tuple = (0.0, 0.0)
) -> np.ndarray:
    """Scan of a single straight wall segment along the x axis."""
    count = max(2, int(np.ceil(length / spacing)) + 1)
    xs = np.linspace(-length / 2.0, length / 2.0, count)
    return np.column_stack([xs + center[0], np.zeros(count) + center[1]])


def add_noise(points: np.ndarray, sigma: float, rng: np.random.Generator) -> np.ndarray:
    """Add isotropic Gaussian noise to a point cloud."""
    return points + rng.normal(0.0, sigma, size=points.shape)


def add_outliers(
    points: np.ndarray,
    count: int,
    rng: np.random.Generator,
    spread: float = 8.0,
) -> np.ndarray:
    """Append ``count`` uniformly distributed outlier points."""
    center = points.mean(axis=0)
    outliers = rng.uniform(-spread, spread, size=(count, 2)) + center
    return np.vstack([points, outliers])


def apply_partial_overlap(
    points: np.ndarray, keep_ratio: float, rng: np.random.Generator
) -> np.ndarray:
    """Keep a random subset, simulating partial overlap between scans."""
    if not 0.0 < keep_ratio <= 1.0:
        raise ValueError("keep_ratio must be in (0, 1]")
    keep = max(3, int(round(keep_ratio * len(points))))
    indices = rng.choice(len(points), size=keep, replace=False)
    return points[np.sort(indices)]


def make_scan_pair(
    world_scan: np.ndarray,
    ground_truth_pose: np.ndarray,
    noise_sigma: float = 0.0,
    outlier_count: int = 0,
    overlap_ratio: float = 1.0,
    seed: int = 0,
    description: str = "synthetic scenario",
) -> SyntheticScenario:
    """Build a (source, target) pair from one world scan and a true pose.

    The target is the clean world scan; the source is the world scan
    transformed by the inverse ground-truth pose, then corrupted with noise,
    outliers and partial overlap as requested.
    """
    rng = np.random.default_rng(seed)
    ground_truth_pose = np.asarray(ground_truth_pose, dtype=float)
    target = world_scan
    source = transform_points(world_scan, _inverse(ground_truth_pose))
    if noise_sigma > 0.0:
        source = add_noise(source, noise_sigma, rng)
    if overlap_ratio < 1.0:
        source = apply_partial_overlap(source, overlap_ratio, rng)
    if outlier_count > 0:
        source = add_outliers(source, outlier_count, rng)
    return SyntheticScenario(
        source=source,
        target=target,
        ground_truth_pose=ground_truth_pose,
        description=description,
    )


def _inverse(pose: np.ndarray) -> np.ndarray:
    """Local copy of the SE(2) inverse to avoid a circular import."""
    c, s = np.cos(pose[2]), np.sin(pose[2])
    t = -np.array([[c, s], [-s, c]]) @ pose[:2]
    return np.array([t[0], t[1], -pose[2]])
