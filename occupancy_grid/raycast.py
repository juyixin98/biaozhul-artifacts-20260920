"""Grid-index ray tracing (Bresenham) and segment-to-map clipping (Liang-Barsky).

All functions operate on integer cell indices or world coordinates and are
pure: they return lists/segments and never mutate grid state.
"""

from __future__ import annotations

from typing import Iterator, Optional, Tuple


def bresenham(x0: int, y0: int, x1: int, y1: int) -> Iterator[Tuple[int, int]]:
    """Yield integer cells on the line from (x0, y0) to (x1, y1), inclusive.

    Standard Bresenham; each cell is yielded exactly once, in order.
    """
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    x, y = x0, y0
    while True:
        yield x, y
        if x == x1 and y == y1:
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy


def clip_segment_to_box(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
) -> Optional[Tuple[float, float, float, float]]:
    """Clip a segment to the axis-aligned box [xmin,xmax] x [ymin,ymax].

    Liang-Barsky algorithm. Returns the clipped segment (cx0, cy0, cx1, cy1),
    or None if the segment lies entirely outside the box.
    """
    dx = x1 - x0
    dy = y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0)):
        if p == 0.0:
            # Segment is parallel to this boundary; reject if outside.
            if q < 0.0:
                return None
            continue
        t = q / p
        if p < 0.0:
            if t > t1:
                return None
            if t > t0:
                t0 = t
        else:
            if t < t0:
                return None
            if t < t1:
                t1 = t
    return (x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy)
