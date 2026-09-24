"""Static grid map with SciPy-based all-goal shortest distances.

Coordinates are (x, y) with x = column, y = row, origin at top-left.
The free-space graph is 4-connected; robots may also wait in place
(waiting is handled by the planners, not by this graph).
"""

from __future__ import annotations

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

Coord = tuple[int, int]


class GridMap:
    def __init__(self, width: int, height: int, obstacles: list[Coord] | None = None):
        if width <= 0 or height <= 0:
            raise ValueError("grid dimensions must be positive")
        self.width = width
        self.height = height
        self.blocked = np.zeros((height, width), dtype=bool)
        for x, y in obstacles or []:
            if not (0 <= x < width and 0 <= y < height):
                raise ValueError(f"obstacle {(x, y)} out of bounds")
            self.blocked[y, x] = True
        self.free_cells: list[Coord] = [
            (x, y)
            for y in range(height)
            for x in range(width)
            if not self.blocked[y, x]
        ]
        self._cell_index = {c: i for i, c in enumerate(self.free_cells)}
        self._graph = self._build_graph()
        self._dist_cache: dict[Coord, np.ndarray] = {}

    def _build_graph(self) -> csr_matrix:
        n = len(self.free_cells)
        rows, cols = [], []
        for c in self.free_cells:
            i = self._cell_index[c]
            for nb in self._raw_neighbors(c):
                rows.append(i)
                cols.append(self._cell_index[nb])
        data = np.ones(len(rows), dtype=np.float64)
        return csr_matrix((data, (rows, cols)), shape=(n, n))

    def _raw_neighbors(self, c: Coord) -> list[Coord]:
        x, y = c
        out = []
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < self.width and 0 <= ny < self.height and not self.blocked[ny, nx]:
                out.append((nx, ny))
        return out

    def is_free(self, c: Coord) -> bool:
        x, y = c
        return 0 <= x < self.width and 0 <= y < self.height and not self.blocked[y, x]

    def neighbors(self, c: Coord) -> list[Coord]:
        """Free 4-connected neighbours plus the cell itself (wait action)."""
        return self._raw_neighbors(c) + [c]

    def distances_to(self, goal: Coord) -> dict[Coord, float]:
        """Shortest-path distance from every free cell to ``goal`` (static grid).

        Computed with scipy's Dijkstra on the free-space graph; used both as
        the A* heuristic and as a connectivity (solvability) pre-check.
        Unreachable cells map to ``np.inf``.
        """
        if goal not in self._cell_index:
            raise ValueError(f"goal {goal} is not a free cell")
        if goal not in self._dist_cache:
            dist = dijkstra(self._graph, directed=False, indices=self._cell_index[goal])
            self._dist_cache[goal] = dist
        dist = self._dist_cache[goal]
        return {c: float(dist[self._cell_index[c]]) for c in self.free_cells}
