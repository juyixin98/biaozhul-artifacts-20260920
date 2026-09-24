"""Occupancy grid with log-odds accumulation.

Coordinate conventions
----------------------
- The world frame is a continuous 2D plane (x, y) in metres; negative
  coordinates are fully supported.
- The grid covers the axis-aligned rectangle
  ``[origin_x, origin_x + width*resolution) x [origin_y, origin_y + height*resolution)``.
  ``(origin_x, origin_y)`` is the world coordinate of the *min corner* of
  cell ``(0, 0)`` and may itself be negative.
- Cell indices: ``ix = floor((x - origin_x) / resolution)`` (same for y).
  A point exactly on the outer max boundary is clamped into the last cell.

Update rules (fixed)
--------------------
- Each ray is a segment from the sensor origin to a measured endpoint.
- The segment is clipped to the map rectangle (Liang-Barsky). A segment
  fully outside the map causes no update.
- Cells before the endpoint along the clipped segment receive the free
  update ``l_free``; the endpoint cell receives the occupied update
  ``l_occ`` — but only when the *unclipped* endpoint lies inside the map
  and the beam actually hit something (``range < max_range``).
- A miss (no return, i.e. ``range >= max_range``) applies only free
  updates along the clipped segment, up to ``max_range``.
- A hit whose endpoint falls outside the map is treated as a miss along
  the clipped portion (no occupied update) — the map cannot store
  evidence beyond its bounds.
- Log-odds are clamped to ``[l_min, l_max]`` after every ray.
- State classification: ``l > 0`` -> OCCUPIED, ``l < 0`` -> FREE,
  ``l == 0`` -> UNKNOWN.
"""

from __future__ import annotations

import enum
import math
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
from scipy.special import expit  # logistic sigmoid: log-odds -> probability

from .raycast import bresenham, clip_segment_to_box


class CellState(enum.IntEnum):
    UNKNOWN = 0
    FREE = 1
    OCCUPIED = 2


# Default log-odds for p_occ=0.7 / p_free=0.4 sensor model.
DEFAULT_L_OCC = math.log(0.7 / 0.3)
DEFAULT_L_FREE = math.log(0.4 / 0.6)
DEFAULT_L_MIN = -4.0
DEFAULT_L_MAX = 4.0


class OccupancyGrid:
    """A 2D occupancy grid storing log-odds in a (height, width) array."""

    def __init__(
        self,
        width: int,
        height: int,
        resolution: float,
        origin_x: float = 0.0,
        origin_y: float = 0.0,
        l_occ: float = DEFAULT_L_OCC,
        l_free: float = DEFAULT_L_FREE,
        l_min: float = DEFAULT_L_MIN,
        l_max: float = DEFAULT_L_MAX,
    ) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        if resolution <= 0:
            raise ValueError("resolution must be positive")
        if l_min >= l_max:
            raise ValueError("l_min must be < l_max")
        self.width = int(width)
        self.height = int(height)
        self.resolution = float(resolution)
        self.origin_x = float(origin_x)
        self.origin_y = float(origin_y)
        self.l_occ = float(l_occ)
        self.l_free = float(l_free)
        self.l_min = float(l_min)
        self.l_max = float(l_max)
        # Row-major: log_odds[iy, ix].
        self.log_odds = np.zeros((self.height, self.width), dtype=np.float64)

    # ------------------------------------------------------------------
    # Coordinate transforms
    # ------------------------------------------------------------------
    @property
    def x_max(self) -> float:
        return self.origin_x + self.width * self.resolution

    @property
    def y_max(self) -> float:
        return self.origin_y + self.height * self.resolution

    def world_to_grid(self, x: float, y: float) -> Optional[Tuple[int, int]]:
        """Map a world point to cell indices, or None if outside the map."""
        ix = math.floor((x - self.origin_x) / self.resolution)
        iy = math.floor((y - self.origin_y) / self.resolution)
        if 0 <= ix < self.width and 0 <= iy < self.height:
            return (ix, iy)
        return None

    def grid_to_world(self, ix: int, iy: int) -> Tuple[float, float]:
        """World coordinates of a cell's centre."""
        return (
            self.origin_x + (ix + 0.5) * self.resolution,
            self.origin_y + (iy + 0.5) * self.resolution,
        )

    def _clamp_index(self, ix: int, iy: int) -> Tuple[int, int]:
        """Clamp indices into the grid (for clipped segment endpoints that
        land exactly on the outer max boundary)."""
        return (
            min(max(ix, 0), self.width - 1),
            min(max(iy, 0), self.height - 1),
        )

    # ------------------------------------------------------------------
    # Ray integration
    # ------------------------------------------------------------------
    def integrate_ray(
        self,
        sensor_x: float,
        sensor_y: float,
        end_x: float,
        end_y: float,
        hit: bool,
    ) -> int:
        """Integrate one ray. Returns the number of cells updated.

        ``hit=True`` means the beam reflected at (end_x, end_y); the
        endpoint cell receives the occupied update only if that endpoint
        lies inside the map.
        """
        clipped = clip_segment_to_box(
            sensor_x, sensor_y, end_x, end_y,
            self.origin_x, self.origin_y, self.x_max, self.y_max,
        )
        if clipped is None:
            return 0
        cx0, cy0, cx1, cy1 = clipped

        start = self.world_to_grid(cx0, cy0)
        end = self.world_to_grid(cx1, cy1)
        # Points exactly on the outer max boundary fall one cell past the
        # edge; clamp them back in.
        if start is None:
            start = self._clamp_index(
                math.floor((cx0 - self.origin_x) / self.resolution),
                math.floor((cy0 - self.origin_y) / self.resolution),
            )
        if end is None:
            end = self._clamp_index(
                math.floor((cx1 - self.origin_x) / self.resolution),
                math.floor((cy1 - self.origin_y) / self.resolution),
            )

        cells = list(bresenham(start[0], start[1], end[0], end[1]))

        # Occupied update only for a genuine hit with the endpoint inside
        # the (unclipped) map.
        endpoint_inside = self.world_to_grid(end_x, end_y) is not None
        mark_occupied = hit and endpoint_inside

        deltas: Dict[Tuple[int, int], float] = {}
        free_cells = cells[:-1] if mark_occupied else cells
        for ix, iy in free_cells:
            key = (ix, iy)
            deltas[key] = deltas.get(key, 0.0) + self.l_free
        if mark_occupied:
            key = cells[-1]
            deltas[key] = deltas.get(key, 0.0) + self.l_occ

        for (ix, iy), delta in deltas.items():
            self.log_odds[iy, ix] = np.clip(
                self.log_odds[iy, ix] + delta, self.l_min, self.l_max
            )
        return len(deltas)

    def integrate_scan(
        self,
        pose_x: float,
        pose_y: float,
        pose_theta: float,
        angles: Sequence[float],
        ranges: Sequence[float],
        max_range: float,
    ) -> int:
        """Integrate a 2D laser scan. Returns total number of cell updates.

        ``angles`` are beam angles relative to the pose heading (radians);
        ``ranges`` are measured distances (metres). A beam with
        ``range >= max_range`` is a miss: the ray is truncated at
        ``max_range`` and only free updates are applied.
        """
        if len(angles) != len(ranges):
            raise ValueError("angles and ranges must have equal length")
        total = 0
        for angle, rng in zip(angles, ranges):
            r = min(float(rng), max_range)
            beam = pose_theta + float(angle)
            end_x = pose_x + r * math.cos(beam)
            end_y = pose_y + r * math.sin(beam)
            total += self.integrate_ray(
                pose_x, pose_y, end_x, end_y, hit=float(rng) < max_range
            )
        return total

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def state_at(self, ix: int, iy: int) -> CellState:
        v = self.log_odds[iy, ix]
        if v > 0.0:
            return CellState.OCCUPIED
        if v < 0.0:
            return CellState.FREE
        return CellState.UNKNOWN

    def state_grid(self) -> np.ndarray:
        """(height, width) int8 array of CellState values."""
        out = np.full(self.log_odds.shape, CellState.UNKNOWN, dtype=np.int8)
        out[self.log_odds < 0.0] = CellState.FREE
        out[self.log_odds > 0.0] = CellState.OCCUPIED
        return out

    def probability_grid(self) -> np.ndarray:
        """(height, width) float array of occupancy probabilities.

        Unknown cells (log-odds == 0) map to exactly 0.5.
        """
        return expit(self.log_odds)
