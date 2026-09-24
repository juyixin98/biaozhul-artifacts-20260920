"""Pure geometry primitives: rectangles, signed distances, collision checks.

Everything here operates on plain numpy arrays.  No I/O, no optimization —
these utilities are shared by the optimizer (constraints) and by the dense
post-solve verifier, so the collision test used for *acceptance* is the same
one used during optimization but applied to the full trajectory.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle [xmin, xmax] x [ymin, ymax]."""

    xmin: float
    ymin: float
    xmax: float
    ymax: float

    @classmethod
    def from_center_wh(cls, cx: float, cy: float, w: float, h: float) -> "Rect":
        return cls(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return self.xmin, self.ymin, self.xmax, self.ymax


def rect_signed_distance(points: np.ndarray, rect: Rect) -> np.ndarray:
    """Signed distance from each point to a rectangle (positive = outside).

    Outside: ordinary Euclidean distance to the boundary.
    Inside:  negative distance to the closest boundary edge
             (-min(w/2-dx, h/2-dy) style value, exact SDF convention).
    """
    pts = np.atleast_2d(np.asarray(points, dtype=float))
    xmin, ymin, xmax, ymax = rect.as_tuple()
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0
    hx = (xmax - xmin) / 2.0
    hy = (ymax - ymin) / 2.0

    qx = np.abs(pts[:, 0] - cx) - hx
    qy = np.abs(pts[:, 1] - cy) - hy
    outside = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0))
    # Inside (both qx,qy <= 0) the signed distance is the (negative)
    # distance to the nearest enclosing face, which is max(qx,qy).
    return np.where(np.maximum(qx, qy) > 0.0, outside, np.maximum(qx, qy))


def rect_sdf_gradients(points: np.ndarray, rect: Rect) -> np.ndarray:
    """Gradient of :func:`rect_signed_distance` w.r.t. each point.

    Returns array of shape (n, 2).  At points exactly on the boundary a
    well-defined outward normal is returned (handled by the ``outside == 0``
    branch choices); degenerate corners/centers get a zero gradient, which is
    numerically safe because such a point already violates the clearance
    constraint and the constraint itself is strictly active only away from
    those measure-zero loci.
    """
    pts = np.atleast_2d(np.asarray(points, dtype=float))
    xmin, ymin, xmax, ymax = rect.as_tuple()
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0
    hx = (xmax - xmin) / 2.0
    hy = (ymax - ymin) / 2.0

    dx = pts[:, 0] - cx
    dy = pts[:, 1] - cy
    qx = np.abs(dx) - hx
    qy = np.abs(dy) - hy
    grad = np.zeros_like(pts)

    outside_mask = np.maximum(qx, qy) > 0.0
    # Outside on an x-face, y-face, or corner — Euclidean distance gradient.
    # On an x-face (qx>0, qy<=0) the gradient is (sign(dx), 0), pointing away
    # from the nearest vertical face; analogously for y-faces.
    ox = np.maximum(qx, 0.0)
    oy = np.maximum(qy, 0.0)
    norm = np.hypot(ox, oy)
    corner = outside_mask & (qx > 0.0) & (qy > 0.0) & (norm > 0.0)
    xface = outside_mask & (qx > 0.0) & (qy <= 0.0)
    yface = outside_mask & (qy > 0.0) & (qx <= 0.0)
    if np.any(corner):
        sx = np.sign(dx[corner])
        sy = np.sign(dy[corner])
        grad[corner, 0] = sx * ox[corner] / norm[corner]
        grad[corner, 1] = sy * oy[corner] / norm[corner]
    if np.any(xface):
        grad[xface, 0] = np.sign(dx[xface])
    if np.any(yface):
        grad[yface, 1] = np.sign(dy[yface])

    # Inside: SDF = max(qx, qy); gradient points toward the *nearest* face,
    # i.e. toward escaping the box (SDF increases toward the boundary).
    inside_mask = ~outside_mask
    if np.any(inside_mask):
        near_x = qx >= qy  # equal -> arbitrary valid face
        ix = inside_mask & near_x
        iy = inside_mask & ~near_x
        if np.any(ix):
            grad[ix, 0] = np.sign(dx[ix])
        if np.any(iy):
            grad[iy, 1] = np.sign(dy[iy])
    return grad


def min_signed_distance(points: np.ndarray, rects: list[Rect]) -> float:
    """Minimum signed distance over all point/rectangle pairs."""
    if not rects:
        return float("inf")
    pts = np.atleast_2d(np.asarray(points, dtype=float))
    worst = float("inf")
    for rect in rects:
        d = rect_signed_distance(pts, rect)
        worst = min(worst, float(np.min(d)))
    return worst


def _segment_distance_sq(a: np.ndarray, b: np.ndarray, p: np.ndarray) -> np.ndarray:
    """Squared distance from point(s) ``p`` to segment a->b (vectorized)."""
    ab = b - a
    len_sq = float(ab @ ab)
    p = np.atleast_2d(p)
    if len_sq <= 1e-30:
        return np.sum((p - a) ** 2, axis=1)
    t = np.clip(((p - a) @ ab) / len_sq, 0.0, 1.0)
    closest = a + t[:, None] * ab
    return np.sum((p - closest) ** 2, axis=1)


def segment_rect_distance(a: np.ndarray, b: np.ndarray, rect: Rect) -> float:
    """Exact signed distance between segment a-b and an axis-aligned rectangle.

    Negative when the segment penetrates the rectangle interior, zero when it
    touches the boundary, positive otherwise.  The segment portion inside the
    box is found exactly by Liang–Barsky clipping; probe points are the clip
    endpoints and the box center lines, which is sufficient to find the exact
    deepest penetration of a straight segment.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    xmin, ymin, xmax, ymax = rect.as_tuple()
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0

    dx = b[0] - a[0]
    dy = b[1] - a[1]
    t_enter, t_leave = 0.0, 1.0
    separated = False
    for p, q in ((-dx, a[0] - xmin), (dx, xmax - a[0]),
                 (-dy, a[1] - ymin), (dy, ymax - a[1])):
        if abs(p) < 1e-30:
            if q < 0.0:
                separated = True  # parallel to a slab and strictly outside
                break
            continue
        r = q / p
        if p < 0.0:
            t_enter = max(t_enter, r)
        else:
            t_leave = min(t_leave, r)

    if not separated and t_enter <= t_leave + 1e-12:
        # Segment has a portion inside (or on) the rectangle.  The piecewise
        # linear interior SDF reaches its minimum at a clip endpoint, where
        # the segment crosses a box center line, or at the projection of the
        # box center onto the segment (when that foot lies on the segment).
        candidates = [0.0, 1.0, t_enter, t_leave]
        if abs(dx) > 1e-30:
            candidates.append((cx - a[0]) / dx)
        if abs(dy) > 1e-30:
            candidates.append((cy - a[1]) / dy)
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq > 1e-30:
            t_foot = ((cx - a[0]) * dx + (cy - a[1]) * dy) / seg_len_sq
            if 0.0 <= t_foot <= 1.0:
                candidates.append(t_foot)
        probe = np.array([a + np.clip(t, 0.0, 1.0) * (b - a)
                          for t in candidates])
        return float(np.min(rect_signed_distance(probe, rect)))

    # No intersection: exact separation = min(endpoint-to-box, corner-to-segment).
    corners = np.array(
        [[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax]], dtype=float
    )
    d_endpoints = rect_signed_distance(np.vstack([a, b]), rect)
    best = float(np.min(d_endpoints))
    corner_ds = np.sqrt([_segment_distance_sq(a, b, c)[0] for c in corners])
    return float(min(best, float(np.min(corner_ds))))


def densify(points: np.ndarray, max_spacing: float) -> np.ndarray:
    """Densify a polyline so consecutive samples are <= ``max_spacing``.

    Endpoints of every original segment are always included (the first point
    is added once; every segment contributes interior subdivisions plus its
    own end), so corners are never skipped.
    """
    pts = np.asarray(points, dtype=float)
    if len(pts) < 2 or max_spacing <= 0:
        return pts
    out = [pts[0]]
    for a, b in zip(pts[:-1], pts[1:]):
        length = float(np.hypot(*(b - a)))
        steps = int(np.ceil(length / max_spacing))
        steps = max(steps, 1)
        for k in range(1, steps + 1):
            t = k / steps
            out.append(a * (1.0 - t) + b * t)
    return np.asarray(out, dtype=float)


def verify_trajectory(
    points: np.ndarray,
    rects: list[Rect],
    clearance: float,
    max_spacing: float,
) -> dict:
    """Collision verification over the *whole* densified trajectory.

    This is the acceptance gate: it samples every segment adaptively (segment
    length / max_spacing) rather than checking only control points, and also
    runs the exact segment/rectangle test on every control segment so a
    collision that is fully contained between two samples cannot slip
    through.

    Returns a report dict; ``ok`` is True iff every sample and every control
    segment satisfies clearance >= ``clearance`` (within a tiny tolerance).
    """
    pts = np.asarray(points, dtype=float)
    dense = densify(pts, max_spacing)
    tol = 1e-9

    # Dense point-based SDF check (the primary gate).
    min_sdf = float("inf")
    worst_sample = None
    for rect in rects:
        d = rect_signed_distance(dense, rect)
        idx = int(np.argmin(d))
        if float(d[idx]) < min_sdf:
            min_sdf = float(d[idx])
            worst_sample = dense[idx]

    # Exact control-segment test catches anything hiding between samples.
    min_seg_dist = float("inf")
    worst_segment = None
    for i, (a, b) in enumerate(zip(pts[:-1], pts[1:])):
        for rect in rects:
            d = segment_rect_distance(a, b, rect)
            if d < min_seg_dist:
                min_seg_dist = float(d)
                worst_segment = i

    ok = (min_sdf + tol >= clearance) and (min_seg_dist + tol >= clearance)
    return {
        "ok": bool(ok),
        "min_clearance": min(min_sdf, min_seg_dist) if rects else float("inf"),
        "min_sample_sdf": min_sdf if rects else float("inf"),
        "min_segment_distance": min_seg_dist if rects else float("inf"),
        "samples_checked": int(len(dense)),
        "control_segments_checked": int(max(len(pts) - 1, 0)),
        "worst_sample": None if worst_sample is None else [float(worst_sample[0]), float(worst_sample[1])],
        "worst_segment_index": worst_segment,
        "sample_spacing": float(max_spacing),
    }


def collapse_consecutive_duplicates(points: np.ndarray, tol: float = 1e-12) -> tuple[np.ndarray, list[list[int]]]:
    """Collapse consecutive near-duplicate points.

    Returns ``(unique_points, groups)`` where ``groups[k]`` lists the original
    indices merged into unique point ``k``.  Non-consecutive revisits (a loop
    that returns to the same location later) are preserved.
    """
    pts = np.asarray(points, dtype=float)
    if len(pts) == 0:
        return pts, []
    keep_idx = [0]
    groups = [[0]]
    for i in range(1, len(pts)):
        if np.hypot(*(pts[i] - pts[keep_idx[-1]])) <= tol:
            groups[-1].append(i)
        else:
            keep_idx.append(i)
            groups.append([i])
    return pts[keep_idx], groups


def expand_to_groups(points: np.ndarray, groups: list[list[int]]) -> np.ndarray:
    """Inverse of :func:`collapse_consecutive_duplicates` (re-expand points)."""
    total = sum(len(g) for g in groups)
    out = np.zeros((total, points.shape[1]), dtype=float)
    for k, g in enumerate(groups):
        for i in g:
            out[i] = points[k]
    return out
