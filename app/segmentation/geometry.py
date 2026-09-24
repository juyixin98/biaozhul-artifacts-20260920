"""Plane geometry helpers implemented with NumPy only.

A plane is represented as ``(n, d)`` where ``n`` is a *unit* normal and the
plane equation is ``n . x + d = 0``.  Every normal is oriented by
``orientation`` ("up" -> n_z >= 0, "down" -> n_z <= 0).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EPS = 1.0e-12


@dataclass(frozen=True)
class Plane:
    """A fitted plane ``n . x + d = 0`` in the caller's coordinate frame."""

    normal: np.ndarray  # shape (3,), unit length
    offset: float       # d
    inlier_count: int
    inlier_ratio: float
    rms: float          # RMS signed distance of the inliers

    @property
    def tilt_deg(self) -> float:
        """Tilt from horizontal, always in ``[0, 90]`` degrees."""
        nz = float(abs(self.normal[2]))
        nz = min(max(nz, 0.0), 1.0)
        return float(np.degrees(np.arccos(nz)))

    def signed_distance(self, points: np.ndarray) -> np.ndarray:
        """Signed distance of every point to the plane (positive above when normal points up)."""
        return points @ self.normal + self.offset


def orient_normal(n: np.ndarray, orientation: str = "up") -> np.ndarray:
    """Flip a unit normal so its z component follows the requested orientation."""
    n = np.asarray(n, dtype=np.float64)
    norm = float(np.linalg.norm(n))
    if norm < EPS:
        raise ValueError("degenerate normal (zero length)")
    n = n / norm
    if orientation == "up" and n[2] < 0.0:
        n = -n
    elif orientation == "down" and n[2] > 0.0:
        n = -n
    return n


def fit_plane(
    points: np.ndarray,
    orientation: str = "up",
    min_span_ratio: float = 1e-9,
) -> Plane:
    """Least-squares plane fit through a set of points.

    Raises ``ValueError`` if fewer than 3 points are given, or the points are
    (numerically) collinear so that no unique plane exists.  Collinearity is
    detected from the ratio of the second-smallest and largest singular
    values of the centred cloud.
    """
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError("points must have shape (N, 3)")
    if len(pts) < 3:
        raise ValueError("at least 3 non-collinear points required")

    centroid = pts.mean(axis=0)
    centered = pts - centroid
    sv, vt = np.linalg.svd(centered, compute_uv=True, full_matrices=False)[1:]
    s_max = float(sv[0])
    if s_max < EPS or float(sv[1]) / s_max < min_span_ratio:
        raise ValueError("points are (nearly) collinear; no unique plane")
    normal = vt[-1]
    if not np.isfinite(normal).all():
        raise ValueError("non-finite plane fit result")
    residual = centered @ normal
    rms = float(np.sqrt(np.mean(residual**2)))
    normal = orient_normal(normal, orientation)
    offset = float(-(normal @ centroid))
    return Plane(
        normal=normal,
        offset=offset,
        inlier_count=len(pts),
        inlier_ratio=1.0,
        rms=rms,
    )


def plane_from_three(points: np.ndarray, orientation: str = "up") -> Plane:
    """Exact plane through three points; raises ``ValueError`` if collinear."""
    pts = np.asarray(points, dtype=np.float64)
    if pts.shape != (3, 3):
        raise ValueError("exactly 3 points of dimension 3 required")
    a, b, c = pts
    normal = np.cross(b - a, c - a)
    if float(np.linalg.norm(normal)) < EPS:
        raise ValueError("three sample points are collinear")
    normal = orient_normal(normal, orientation)
    d = float(-(normal @ a))
    return Plane(normal=normal, offset=d, inlier_count=3, inlier_ratio=1.0, rms=0.0)


def local_shape_descriptors(
    points: np.ndarray,
    k: int = 8,
    orientation: str = "up",
    max_query_points: int = 4000,
    linearity_ratio: float = 0.15,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-point local normal plus a line/feature flag.

    For each point's k-nearest (distinct) neighbours the three singular
    values ``s1 >= s2 >= s3`` of the centred neighbourhood describe its shape:

    * ground patch : ``s2 ~ s1``, ``s3`` small  -> normal well defined
    * wall/pole    : ``s2`` small (1-D dominated neighbourhood) -> ``linear``
    * sparse/degenerate neighbourhood        -> ``valid`` is False

    Returns ``(normals, valid, linear)``.  Invalid entries hold a zero normal
    and ``linear=False``.
    """
    pts = np.asarray(points, dtype=np.float64)
    n = len(pts)
    normals = np.zeros((n, 3), dtype=np.float64)
    valid = np.zeros(n, dtype=bool)
    linear = np.zeros(n, dtype=bool)
    if n < 3 or k < 3:
        return normals, valid, linear

    q = min(n, max_query_points)
    for start in range(0, n, q):
        stop = min(start + q, n)
        block = pts[start:stop]
        d2 = (
            np.sum(block**2, axis=1)[:, None]
            - 2.0 * (block @ pts.T)
            + np.sum(pts**2, axis=1)[None, :]
        )
        np.maximum(d2, 0.0, out=d2)
        kk = min(k, n)
        idx = np.argpartition(d2, kth=kk - 1, axis=1)[:, :kk]
        for li, gi in enumerate(range(start, stop)):
            neigh = pts[idx[li]]
            uniq = np.unique(neigh, axis=0)
            if len(uniq) < 3:
                continue
            centroid = uniq.mean(axis=0)
            centered = uniq - centroid
            try:
                sv, vt = np.linalg.svd(centered, compute_uv=True, full_matrices=False)[1:]
            except np.linalg.LinAlgError:
                continue
            s_max = float(sv[0])
            if s_max < EPS:
                continue
            # A 1-D (line/edge) neighbourhood: flag it but keep no normal.
            if float(sv[1]) / s_max < linearity_ratio:
                linear[gi] = True
                continue
            nrm = vt[-1]
            normals[gi] = orient_normal(nrm, orientation)
            valid[gi] = True
    return normals, valid, linear


def local_normals(
    points: np.ndarray,
    k: int = 8,
    orientation: str = "up",
    max_query_points: int = 4000,
) -> tuple[np.ndarray, np.ndarray]:
    """Backward-compatible wrapper around :func:`local_shape_descriptors`."""
    normals, valid, _ = local_shape_descriptors(
        points, k=k, orientation=orientation, max_query_points=max_query_points
    )
    return normals, valid
