"""Obstacle-aware 2D heuristic.

A Dijkstra flood from the goal cell over the 8-connected grid (with
cells inflated by the vehicle footprint radius) gives a
kinematics-ignoring but obstacle-aware lower bound on the remaining
driving distance. Where the flood cannot reach, we fall back to the
Euclidean distance, which is still admissible.
"""

from __future__ import annotations

import heapq
import math

import numpy as np

from .grid_map import GridMap

_SQRT2 = math.sqrt(2.0)
# 8-connected neighborhood: (drow, dcol, cost_factor)
_NEIGHBORS = [
    (-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
    (-1, -1, _SQRT2), (-1, 1, _SQRT2), (1, -1, _SQRT2), (1, 1, _SQRT2),
]


class GridHeuristic:
    """Precomputed shortest-path distance (m) from every cell to the goal."""

    def __init__(self, grid: GridMap, goal_xy: tuple[float, float], inflation: float):
        self.grid = grid
        self.goal_xy = goal_xy
        blocked = grid.clearance < inflation
        dist = np.full(grid.shape, math.inf, dtype=np.float64)
        grow, gcol = grid.world_to_cell(*goal_xy)
        if grid.in_bounds(grow, gcol) and not blocked[grow, gcol]:
            dist[grow, gcol] = 0.0
            pq = [(0.0, grow, gcol)]
            while pq:
                d, r, c = heapq.heappop(pq)
                if d > dist[r, c]:
                    continue
                for dr, dc, w in _NEIGHBORS:
                    nr, nc = r + dr, c + dc
                    if not grid.in_bounds(nr, nc) or blocked[nr, nc]:
                        continue
                    nd = d + w * grid.resolution
                    if nd < dist[nr, nc]:
                        dist[nr, nc] = nd
                        heapq.heappush(pq, (nd, nr, nc))
        self._dist = dist

    def cost_to_go(self, x: float, y: float) -> float:
        """Admissible estimate of remaining cost from a world point."""
        row, col = self.grid.world_to_cell(x, y)
        if self.grid.in_bounds(row, col) and math.isfinite(self._dist[row, col]):
            return float(self._dist[row, col])
        # Unreached by the flood (or out of bounds): Euclidean fallback.
        gx, gy = self.goal_xy
        return math.hypot(x - gx, y - gy)
