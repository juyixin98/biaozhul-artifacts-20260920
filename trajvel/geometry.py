"""Polyline geometry helpers.

All functions are pure NumPy and safe against degenerate (zero-length)
segments: no division by a segment length happens without an eps guard.
"""

from __future__ import annotations

import numpy as np

#: Distances below this are treated as zero (machine-precision safe).
EPS = 1e-12


def as_points(waypoints: object) -> np.ndarray:
    """Validate and convert waypoints to an (N, D) float array.

    Raises ValueError on malformed input (wrong shape, non-finite values,
    fewer than two points).
    """
    points = np.asarray(waypoints, dtype=float)
    if points.ndim != 2 or points.shape[0] < 2 or points.shape[1] < 1:
        raise ValueError(
            "waypoints must be an array of shape (N>=2, D>=1); "
            f"got shape {points.shape}"
        )
    if not np.all(np.isfinite(points)):
        raise ValueError("waypoints must be finite numbers")
    return points


def segment_lengths(points: np.ndarray) -> np.ndarray:
    """Euclidean length of each of the N-1 segments. Zero-length is allowed."""
    return np.linalg.norm(np.diff(points, axis=0), axis=1)


def dedupe_points(points: np.ndarray, tol: float = EPS) -> tuple[np.ndarray, int]:
    """Remove consecutive duplicate points (zero-length segments).

    Returns (deduped_points, removed_count). The first point is always
    kept. If everything collapses to a single point, the first two
    original points are kept so downstream code still sees N>=2.
    """
    lengths = segment_lengths(points)
    keep = np.ones(len(points), dtype=bool)
    keep[1:] = lengths > tol
    deduped = points[keep]
    removed = int(len(points) - len(deduped))
    if len(deduped) < 2:
        deduped = points[:2]
        removed = int(len(points) - 2)
    return deduped, removed


def unit_directions(points: np.ndarray, lengths: np.ndarray) -> np.ndarray:
    """Unit direction per segment; zero-length segments get a zero vector."""
    diffs = np.diff(points, axis=0)
    safe = np.where(lengths > EPS, lengths, 1.0)
    return diffs / safe[:, None]


def turning_angles(points: np.ndarray) -> np.ndarray:
    """Turning angle (radians, [0, pi]) at every node.

    Endpoints get 0. A node adjacent to a zero-length segment also gets 0
    (no well-defined direction change; such nodes are normally removed by
    dedupe_points first).
    """
    n = len(points)
    angles = np.zeros(n)
    if n < 3:
        return angles
    lengths = segment_lengths(points)
    directions = unit_directions(points, lengths)
    for i in range(1, n - 1):
        d0, d1 = directions[i - 1], directions[i]
        if lengths[i - 1] <= EPS or lengths[i] <= EPS:
            continue
        cos_angle = float(np.clip(np.dot(d0, d1), -1.0, 1.0))
        angles[i] = float(np.arccos(cos_angle))
    return angles
