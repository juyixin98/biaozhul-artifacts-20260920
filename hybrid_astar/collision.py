"""Collision checking against the clearance field."""

from __future__ import annotations

import numpy as np

from .grid_map import GridMap
from .vehicle import Vehicle


class CollisionChecker:
    """Checks vehicle poses against a GridMap using footprint circles."""

    def __init__(self, grid: GridMap, vehicle: Vehicle):
        self.grid = grid
        self.vehicle = vehicle
        self.radius = vehicle.circle_radius

    def is_pose_free(self, x: float, y: float, theta: float) -> bool:
        """True if every footprint circle has enough clearance."""
        for cx, cy in self.vehicle.circle_centers(x, y, theta):
            if self.grid.clearance_at(cx, cy) < self.radius:
                return False
        return True

    def is_path_free(self, xs: np.ndarray, ys: np.ndarray, ths: np.ndarray) -> bool:
        """True if all densely sampled poses are collision-free."""
        for x, y, t in zip(xs, ys, ths):
            if not self.is_pose_free(float(x), float(y), float(t)):
                return False
        return True
