"""Deterministic 2D ray traversal and clipping rules.

Conventions (fixed, see README "坐标与裁剪约定"):

* World coordinates are continuous meters. A cell index ``(ix, iy)`` covers the
  continuous region ``[origin_x + ix*res, origin_x + (ix+1)*res)`` on x and
  likewise on y. Mapping world -> cell index is therefore
  ``ix = floor((x - origin_x) / res)``. A point exactly on a cell boundary is
  assigned to the cell with the *higher* index.
* Rays are clipped against the map rectangle with Liang–Barsky in continuous
  cell coordinates before traversal.
* Traversal is an Amanatides–Woo grid walk (super-cover style): when a ray
  crosses exactly through an *interior* cell corner, both side cells and the
  diagonal cell are visited (ties are broken y-side first, deterministically).
  A ray whose *endpoint* lies exactly on a corner contributes the endpoint cell
  only. Cells are always visited exactly once.
* A ray with an echo marks every traversed cell as free and the echo endpoint
  cell as occupied; if the endpoint lies outside the map, the occupied update
  is dropped and only the clipped (free) segment is integrated.
* A ray without an echo (no return) marks the whole traversed segment free and
  never marks any cell occupied.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EPS = 1e-9


@dataclass(frozen=True)
class RayObservation:
    """One ray measurement with an echo, in world coordinates.

    The echo endpoint is given either directly as ``(ex, ey)`` or derived from
    ``angle`` (radians) + ``max_range`` (metres). A *no-return* observation is
    integrated separately via ``OccupancyGrid.update_no_return``.
    """

    ox: float
    oy: float
    ex: float | None = None
    ey: float | None = None
    angle: float | None = None
    max_range: float | None = None

    def resolved_endpoint(self) -> tuple[float, float] | None:
        """Return the physical endpoint, deriving it from angle/max_range."""
        if self.ex is not None and self.ey is not None:
            return float(self.ex), float(self.ey)
        if self.angle is not None and self.max_range is not None:
            r = float(self.max_range)
            return (
                float(self.ox) + r * float(np.cos(self.angle)),
                float(self.oy) + r * float(np.sin(self.angle)),
            )
        return None


def _liang_barsky(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    nx: int,
    ny: int,
) -> tuple[float, float, float, float] | None:
    """Clip segment to rectangle ``[0, nx] x [0, ny]`` in cell coordinates.

    Returns clipped endpoints ``(x0, y0, x1, y1)`` or ``None`` if the segment
    does not intersect the rectangle interior/boundary.
    """
    dx = x1 - x0
    dy = y1 - y0
    t_low, t_high = 0.0, 1.0
    for p, q in ((-dx, x0), (dx, nx - x0), (-dy, y0), (dy, ny - y0)):
        if abs(p) < EPS:
            # Segment parallel to this boundary; touching it (q == 0) is inside.
            if q < -EPS:
                return None
            continue
        t = q / p
        if p < 0:
            if t > t_high + EPS:
                return None
            if t > t_low:
                t_low = t
        else:
            if t < t_low - EPS:
                return None
            if t < t_high:
                t_high = t
    return (
        x0 + t_low * dx,
        y0 + t_low * dy,
        x0 + t_high * dx,
        y0 + t_high * dy,
    )


def traverse_cells(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    nx: int,
    ny: int,
) -> list[tuple[int, int]]:
    """Walk all grid cells intersected by a continuous segment.

    Inputs are in *cell coordinates* (world mapped by
    ``floor((w-origin)/res)``). The segment is first clipped to the map.
    Returns unique integer cell indices in traversal order.

    Rules:
    * Endpoints on cell boundaries belong to the higher-indexed cell
      (floor mapping); a clipped endpoint exactly on the outer map boundary is
      excluded because its floor index is out of range.
    * Interior corner crossings visit both side cells and the diagonal cell.
    * The starting cell of a zero-length segment inside the map is returned.
    """
    clipped = _liang_barsky(float(x0), float(y0), float(x1), float(y1), nx, ny)
    if clipped is None:
        return []
    cx0, cy0, cx1, cy1 = clipped

    dx = cx1 - cx0
    dy = cy1 - cy0

    # Zero-length clipped segment: single cell if it lies on/inside the map.
    if abs(dx) < EPS and abs(dy) < EPS:
        ix = int(np.floor(cx0))
        iy = int(np.floor(cy0))
        if 0 <= ix < nx and 0 <= iy < ny:
            return [(ix, iy)]
        return []

    ix = int(np.floor(cx0))
    iy = int(np.floor(cy0))

    step_x = 1 if dx > 0 else -1
    step_y = 1 if dy > 0 else -1

    # Distance (in t) until the first vertical/horizontal grid line.
    if abs(dx) > EPS:
        boundary_x = ix + 1 if step_x > 0 else ix
        t_max_x = (boundary_x - cx0) / dx
        t_delta_x = abs(1.0 / dx)
    else:
        t_max_x = float("inf")
        t_delta_x = float("inf")
    if abs(dy) > EPS:
        boundary_y = iy + 1 if step_y > 0 else iy
        t_max_y = (boundary_y - cy0) / dy
        t_delta_y = abs(1.0 / dy)
    else:
        t_max_y = float("inf")
        t_delta_y = float("inf")

    cells: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    def add(cx: int, cy: int) -> None:
        if 0 <= cx < nx and 0 <= cy < ny and (cx, cy) not in seen:
            seen.add((cx, cy))
            cells.append((cx, cy))

    add(ix, iy)
    # Walk until we reach/pass the endpoint. A crossing exactly at t=1 is the
    # endpoint: do not step into side cells (a terminal corner contributes the
    # endpoint cell only); the explicit endpoint add below handles it.
    while min(t_max_x, t_max_y) <= 1.0 + EPS:
        if t_max_y < t_max_x - EPS:
            if t_max_y >= 1.0 - EPS:
                break
            t_max_y += t_delta_y
            iy += step_y
        elif t_max_x < t_max_y - EPS:
            if t_max_x >= 1.0 - EPS:
                break
            t_max_x += t_delta_x
            ix += step_x
        else:
            # Exact interior corner crossing (super-cover): visit both side
            # cells and the diagonal cell; y-side first, then x-side.
            if t_max_x >= 1.0 - EPS:
                break
            t_max_y += t_delta_y
            iy += step_y
            add(ix, iy)  # y-side cell
            iy -= step_y
            t_max_x += t_delta_x
            ix += step_x
            add(ix, iy)  # x-side cell
            iy += step_y
        add(ix, iy)

    # Endpoint cell (boundary points map via floor to the higher-index cell).
    add(int(np.floor(cx1)), int(np.floor(cy1)))
    return cells
