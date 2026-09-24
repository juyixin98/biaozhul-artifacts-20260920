"""Occupancy grid map with a precomputed clearance (distance) field."""

from __future__ import annotations

import math

import numpy as np
from scipy.ndimage import distance_transform_edt


class GridMap:
    """2D occupancy grid.

    Cell value 1 means occupied (obstacle), 0 means free. The clearance
    field stores, for every free cell, the distance in meters to the
    nearest occupied cell, computed with an exact Euclidean distance
    transform (scipy).
    """

    def __init__(
        self,
        data,
        resolution: float = 0.5,
        origin: tuple[float, float] = (0.0, 0.0),
    ):
        arr = np.asarray(data, dtype=np.uint8)
        if arr.ndim != 2:
            raise ValueError("map data must be a 2D array")
        if resolution <= 0:
            raise ValueError("resolution must be positive")
        self.data = arr
        self.resolution = float(resolution)
        self.origin = (float(origin[0]), float(origin[1]))
        # Distance from each free cell to the closest obstacle cell.
        self.clearance = distance_transform_edt(1 - arr) * self.resolution

    @property
    def shape(self) -> tuple[int, int]:
        """(rows, cols) i.e. (ny, nx)."""
        return self.data.shape

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        """World coordinates -> (row, col) cell indices (nearest)."""
        col = int(round((x - self.origin[0]) / self.resolution))
        row = int(round((y - self.origin[1]) / self.resolution))
        return row, col

    def in_bounds(self, row: int, col: int) -> bool:
        return 0 <= row < self.data.shape[0] and 0 <= col < self.data.shape[1]

    def clearance_at(self, x: float, y: float) -> float:
        """Clearance (m) at a world point; -inf outside the map."""
        row, col = self.world_to_cell(x, y)
        if not self.in_bounds(row, col):
            return -math.inf
        return float(self.clearance[row, col])

    def is_occupied(self, x: float, y: float) -> bool:
        """True if the world point is outside the map or in an obstacle."""
        row, col = self.world_to_cell(x, y)
        if not self.in_bounds(row, col):
            return True
        return bool(self.data[row, col])
