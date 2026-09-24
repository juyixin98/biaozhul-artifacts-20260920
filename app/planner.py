"""Core grid, D* Lite planner and independent Dijkstra baseline.

Algorithm
---------
Incremental planning uses D* Lite (Koenig & Likhachev, AAMO 2002), the
standard algorithm for a fixed goal with moving start and edge-cost changes.
Every produced cost is cross-checked inside this module against an
*independent* Dijkstra implementation (no shared incremental state) so the
service can honestly report whether the incremental result is optimal.

Grid semantics
--------------
* ``cost[y, x]`` is the non-negative weight paid to *enter* cell (x, y);
  the start cell is free (path cost = sum of entered-cell weights).
* ``blocked[y, x]`` cells are obstacles: they cannot be entered and no edge
  may have an obstacle as an endpoint.
* ``connectivity`` 4: orthogonal neighbours only; 8: orthogonal + diagonal.
* Diagonal corner rule (``diagonal_rule="two_blocked"``): a diagonal move
  from (x, y) to (x+dx, y+dy) is forbidden when *both* orthogonally
  adjacent cells (x+dx, y) and (x, y+dy) are blocked -- the move would cut
  through the corner formed by two obstacles. With one blocked side the move
  is allowed (sliding along a wall). The rule is applied identically by
  D* Lite and by Dijkstra.
* Negative weights are rejected; zero-weight cells are allowed (the
  admissible heuristic scales with the smallest positive finite weight).
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Iterator, Optional

import numpy as np

__all__ = [
    "PlanResult",
    "Grid",
    "DStarLite",
    "dijkstra_cost",
    "dijkstra_path",
    "CostValidationError",
]

INF = float("inf")
_EPS = 1e-12

# (dx, dy), ordered: orthogonal first, then diagonals; deterministic order is
# used for path extraction tie-breaking.
_NEIGHBOR_OFFSETS = (
    (1, 0),
    (0, 1),
    (-1, 0),
    (0, -1),
    (1, 1),
    (-1, 1),
    (-1, -1),
    (1, -1),
)
_ORTHOGONAL_COUNT = 4


class CostValidationError(ValueError):
    """Raised when a cost array or update contains illegal values."""


@dataclass
class PlanResult:
    """Outcome of one D* Lite planning call.

    ``reexpanded_nodes`` counts vertices actually popped from the priority
    queue with an up-to-date key during this call -- a diagnostic of how much
    state the incremental planner had to revisit, *not* a performance
    guarantee.
    """

    path: Optional[list[tuple[int, int]]]
    cost: float
    reachable: bool
    reexpanded_nodes: int
    vertex_updates: int
    diagnostics: dict = field(default_factory=dict)


class Grid:
    """Read/write weighted 2D grid shared by both planners.

    The D* Lite instance mutates only its own g/rhs state, never the grid.
    """

    def __init__(
        self,
        cost: np.ndarray,
        blocked: np.ndarray,
        connectivity: int = 8,
        diagonal_rule: str = "two_blocked",
    ):
        cost = np.asarray(cost, dtype=np.float64)
        blocked = np.asarray(blocked, dtype=bool)
        if cost.ndim != 2 or blocked.ndim != 2:
            raise CostValidationError("cost/blocked must be 2-D arrays")
        if cost.shape != blocked.shape:
            raise CostValidationError("cost and blocked must share a shape")
        if np.any(~np.isfinite(cost)):
            raise CostValidationError("all cell costs must be finite")
        if np.any(cost < 0.0):
            raise CostValidationError("negative cell costs are forbidden")
        if connectivity not in (4, 8):
            raise CostValidationError("connectivity must be 4 or 8")
        if diagonal_rule != "two_blocked":
            raise CostValidationError("only the 'two_blocked' corner rule is implemented")

        self.height, self.width = cost.shape
        self.connectivity = connectivity
        self.diagonal_rule = diagonal_rule
        self.cost = cost.copy()
        self.blocked = blocked.copy()
        # Smallest finite cell weight; admissible lower bound on one grid
        # step (every move enters exactly one cell and pays its weight).
        # Zero is allowed and makes the heuristic identically zero, which
        # stays consistent in the presence of zero-cost edges.
        self.min_weight = float(cost.min()) if cost.size else INF

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #
    def update(self, cost: Optional[np.ndarray], blocked: Optional[np.ndarray]) -> None:
        """Overwrite cost/blocked arrays wholesale (used on map revision)."""
        if cost is not None:
            cost = np.asarray(cost, dtype=np.float64)
            if cost.shape != (self.height, self.width):
                raise CostValidationError("cost patch shape mismatch")
            if np.any(~np.isfinite(cost)):
                raise CostValidationError("all cell costs must be finite")
            if np.any(cost < 0.0):
                raise CostValidationError("negative cell costs are forbidden")
            self.cost = cost.copy()
            self.min_weight = float(cost.min()) if cost.size else INF
        if blocked is not None:
            blocked = np.asarray(blocked, dtype=bool)
            if blocked.shape != (self.height, self.width):
                raise CostValidationError("blocked patch shape mismatch")
            self.blocked = blocked.copy()

    # ------------------------------------------------------------------ #
    # Geometry
    # ------------------------------------------------------------------ #
    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def is_blocked(self, x: int, y: int) -> bool:
        return bool(self.blocked[y, x])

    def _diagonal_open(self, x: int, y: int, dx: int, dy: int) -> bool:
        # two_blocked: forbidden only if BOTH orthogonal cells are blocked
        # or out of bounds (out-of-bounds is impassable like an obstacle).
        side_a_blocked = (not self.in_bounds(x + dx, y)) or self.blocked[y, x + dx]
        side_b_blocked = (not self.in_bounds(x, y + dy)) or self.blocked[y + dy, x]
        return not (side_a_blocked and side_b_blocked)

    def neighbors(self, x: int, y: int) -> Iterator[tuple[int, int, float]]:
        """Yield (nx, ny, step_length) for every *statically possible* move.

        Obstacles of endpoints and the corner rule are checked by
        :meth:`edge_cost`; this iterator only applies bounds and
        connectivity, so D* Lite can evaluate edges whose status changes.
        """
        count = len(_NEIGHBOR_OFFSETS) if self.connectivity == 8 else _ORTHOGONAL_COUNT
        for i in range(count):
            dx, dy = _NEIGHBOR_OFFSETS[i]
            nx, ny = x + dx, y + dy
            if not self.in_bounds(nx, ny):
                continue
            length = np.hypot(dx, dy)
            yield nx, ny, float(length)

    def edge_cost(self, u: tuple[int, int], v: tuple[int, int]) -> float:
        """True directed edge cost u -> v under the *current* grid.

        inf when either endpoint is blocked, or v is out of bounds, or the
        diagonal corner rule blocks the move.
        """
        x, y = u
        nx, ny = v
        if not self.in_bounds(x, y) or not self.in_bounds(nx, ny):
            return INF
        if self.blocked[y, x] or self.blocked[ny, nx]:
            return INF
        dx, dy = nx - x, ny - y
        manhattan = abs(dx) + abs(dy)
        if dx != 0 and dy != 0:
            if self.connectivity != 8 or manhattan != 2:
                return INF
            if not self._diagonal_open(x, y, dx, dy):
                return INF
        elif manhattan != 1:
            return INF
        return float(self.cost[ny, nx])


# ---------------------------------------------------------------------- #
# D* Lite
# ---------------------------------------------------------------------- #
class DStarLite:
    """D* Lite over a :class:`Grid`, goal fixed, start free to move.

    Coordinates are flattened to vertex ids: ``id = y * width + x``.
    """

    def __init__(self, grid: Grid, start: tuple[int, int], goal: tuple[int, int]):
        self.grid = grid
        h, w = grid.height, grid.width
        self.size = h * w
        self.start = self._vid(*start)
        self.goal = self._vid(*goal)
        if not grid.in_bounds(*start) or grid.is_blocked(*start):
            raise CostValidationError(f"start cell {start} must be in bounds and free")
        if not grid.in_bounds(*goal) or grid.is_blocked(*goal):
            raise CostValidationError(f"goal cell {goal} must be in bounds and free")
        if start == goal:
            raise CostValidationError("start and goal must differ")

        self.g = np.full(self.size, INF, dtype=np.float64)
        self.rhs = np.full(self.size, INF, dtype=np.float64)
        self.km = 0.0
        self.heap: list[tuple[float, float, int, int]] = []
        # Current key per vertex; entries not matching are stale.
        self._key_g = np.full(self.size, INF, dtype=np.float64)
        self._key_rhs = np.full(self.size, INF, dtype=np.float64)
        self._in_heap = np.zeros(self.size, dtype=bool)
        self._counter = 0
        # Per-call diagnostics
        self.last_reexpanded = 0
        self.last_vertex_updates = 0
        # Heuristic scale fixed at the historical minimum cell weight. D*
        # Lite requires a fixed consistent heuristic; edge weights never
        # drop below this while it stays valid. If a later update introduces
        # a smaller weight, :meth:`edge_costs_changed` rebuilds all keys.
        self._w_base = self.grid.min_weight
        self.rhs[self.goal] = 0.0
        self._update_vertex(self.goal)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _vid(self, x: int, y: int) -> int:
        return y * self.grid.width + x

    def _xy(self, vid: int) -> tuple[int, int]:
        return vid % self.grid.width, vid // self.grid.width

    def h(self, vid: int) -> float:
        """Admissible, consistent heuristic distance to ``self.start``.

        One move enters exactly one cell and costs that cell's weight, whose
        global lower bound is ``grid.min_weight``. With 8-connectivity at
        least Chebyshev distance moves are required from any cell to the
        start (4-connectivity: Manhattan), so ``min_weight`` times that
        number of moves never over-estimates and satisfies the consistency
        inequality h(u) <= c(u,v) + h(v) on every open edge. When
        ``min_weight`` is 0 the heuristic is identically 0 (still consistent,
        which matters because zero-cost edges would violate any positive
        distance-based heuristic).
        """
        w = self._w_base
        if w <= 0.0 or not np.isfinite(w):
            return 0.0
        x, y = self._xy(vid)
        sx, sy = self._xy(self.start)
        dx, dy = abs(x - sx), abs(y - sy)
        if self.grid.connectivity == 8:
            steps = max(dx, dy)
        else:
            steps = dx + dy
        return float(w) * float(steps)

    def _key(self, vid: int) -> tuple[float, float]:
        k2 = self.g[vid]
        k1 = min(self.g[vid], self.rhs[vid]) + self.h(vid) + self.km
        return k1, k2

    @staticmethod
    def _consistent(gv: float, rhsv: float) -> bool:
        """Locally consistent check safe for (inf, inf): inf-inf is NaN."""
        if gv == INF and rhsv == INF:
            return True
        return abs(gv - rhsv) <= _EPS

    def _update_vertex(self, vid: int) -> None:
        self.last_vertex_updates += 1
        if vid != self.goal:
            best = INF
            x, y = self._xy(vid)
            for nx, ny, _ in self.grid.neighbors(x, y):
                s = self._vid(nx, ny)
                c = self.grid.edge_cost((x, y), (nx, ny))
                val = self.g[s] + c
                if val < best:
                    best = val
            self.rhs[vid] = best
        if not self._consistent(self.g[vid], self.rhs[vid]):
            key = self._key(vid)
            self._key_g[vid], self._key_rhs[vid] = key
            self._in_heap[vid] = True
            self._counter += 1
            heapq.heappush(self.heap, (key[0], key[1], self._counter, vid))
        else:
            self._in_heap[vid] = False

    def _entry_current(self, k1: float, k2: float, vid: int) -> bool:
        """Whether heap entry (k1,k2,vid) is still the vertex's live key."""
        return (
            self._in_heap[vid]
            and k1 == self._key_g[vid]
            and k2 == self._key_rhs[vid]
            and not self._consistent(self.g[vid], self.rhs[vid])
        )

    def _drop_stale_top(self) -> None:
        """Pop stale entries off the heap top; leave the live top in place."""
        while self.heap:
            k1, k2, _, vid = self.heap[0]
            if self._entry_current(k1, k2, vid):
                return
            heapq.heappop(self.heap)

    def _peek_key(self) -> tuple[float, float]:
        self._drop_stale_top()
        if not self.heap:
            return (INF, INF)
        k1, k2, _, _ = self.heap[0]
        return (k1, k2)

    def _pop_min(self) -> Optional[tuple[tuple[float, float], int]]:
        self._drop_stale_top()
        if not self.heap:
            return None
        k1, k2, _, vid = heapq.heappop(self.heap)
        self._in_heap[vid] = False
        return (k1, k2), vid

    def _preds(self, vid: int) -> Iterator[int]:
        """Predecessors = symmetric neighbour set (all edges undirected)."""
        x, y = self._xy(vid)
        for nx, ny, _ in self.grid.neighbors(x, y):
            yield self._vid(nx, ny)

    # ------------------------------------------------------------------ #
    # D* Lite main procedure (ComputeShortestPath)
    # ------------------------------------------------------------------ #
    def compute_shortest_path(self) -> None:
        self.last_reexpanded = 0
        self.last_vertex_updates = 0
        while True:
            # Canonical D* Lite termination: stop when the start is locally
            # consistent and no queue entry can improve the start's key.
            k_old = self._peek_key()
            start_key = self._key(self.start)
            start_consistent = self._consistent(self.g[self.start], self.rhs[self.start])
            if k_old[0] == INF:
                break  # empty queue: nothing left to repair
            if k_old >= start_key and start_consistent:
                break
            item = self._pop_min()
            if item is None:
                break
            k_old, u = item
            self.last_reexpanded += 1
            k_new = self._key(u)
            if k_old < k_new:
                # Key got larger while queued (km/start changed): re-queue.
                self._key_g[u], self._key_rhs[u] = k_new
                self._in_heap[u] = True
                self._counter += 1
                heapq.heappush(self.heap, (k_new[0], k_new[1], self._counter, u))
            elif self.g[u] > self.rhs[u] + _EPS:
                # Overconsistent: locally optimal immediately.
                self.g[u] = self.rhs[u]
                for s in self._preds(u):
                    self._update_vertex(s)
            else:
                # Underconsistent: invalidate, then repair u and predecessors.
                self.g[u] = INF
                self._update_vertex(u)
                for s in self._preds(u):
                    self._update_vertex(s)

    # ------------------------------------------------------------------ #
    # Public incremental operations
    # ------------------------------------------------------------------ #
    def edge_costs_changed(self, changed_cells: list[tuple[int, int]]) -> None:
        """Feed an edge-cost change set to D* Lite (Section IV of the paper).

        ``changed_cells`` lists every cell whose own weight or blocked flag
        was touched. On an undirected grid the affected edge set is, for each
        such cell c, every edge incident to c -- i.e. candidate vertices
        {c} union neighbours(c). That covers:

        * c's own entering edges (weight/block change);
        * edges from c to its diagonals (corner-rule status);
        * edges between c and neighbours that open/close when c is toggled,
          including the orthogonal "other side" of a diagonal corner.
        """
        affected: set[int] = set()
        for cx, cy in changed_cells:
            if not self.grid.in_bounds(cx, cy):
                continue
            affected.add(self._vid(cx, cy))
            for nx, ny, _ in self.grid.neighbors(cx, cy):
                affected.add(self._vid(nx, ny))
        for vid in affected:
            self._update_vertex(vid)

        # If the smallest cell weight shrank, the heuristic scale changed
        # and keys computed under the old scale are no longer valid. Rebuild
        # every inconsistent vertex's key with km reset to 0 (g/rhs stay
        # intact -- they do not depend on the heuristic). This is a
        # conservative O(N) repair; weight-*decreasing* map edits are the
        # only case that needs it.
        new_min = self.grid.min_weight
        if (not np.isfinite(self._w_base)) or (
            np.isfinite(new_min) and new_min + _EPS < self._w_base
        ):
            self._w_base = new_min if np.isfinite(new_min) else self._w_base
            if new_min <= 0.0:
                self._w_base = 0.0
            self.km = 0.0
            self.heap.clear()
            self._in_heap.fill(False)
            self._counter = 0
            for vid in range(self.size):
                if not self._consistent(self.g[vid], self.rhs[vid]):
                    key = self._key(vid)
                    self._key_g[vid], self._key_rhs[vid] = key
                    self._in_heap[vid] = True
                    self._counter += 1
                    heapq.heappush(
                        self.heap, (key[0], key[1], self._counter, vid)
                    )

    def move_start(self, new_start: tuple[int, int]) -> None:
        """Move the start (any distance). Implemented as the D* Lite
        start-move update: km accumulates the true distance travelled,
        heuristic keys shift with ``self.start`` automatically.
        """
        if not self.grid.in_bounds(*new_start):
            raise CostValidationError(f"start {new_start} out of bounds")
        if self.grid.is_blocked(*new_start):
            raise CostValidationError(f"start {new_start} is blocked")
        ns = self._vid(*new_start)
        if ns == self.goal:
            raise CostValidationError("start cannot equal goal")
        if ns != self.start:
            d = self.h(ns)  # h measured from old start before swapping
            self.km += d
            self.start = ns

    def plan(self) -> PlanResult:
        """Run the incremental repair and extract a greedy path."""
        self.compute_shortest_path()
        cost = self.rhs[self.start]
        reachable = bool(np.isfinite(cost))
        path = self._extract_path() if reachable else None
        return PlanResult(
            path=path,
            cost=float(cost) if reachable else INF,
            reachable=reachable,
            reexpanded_nodes=self.last_reexpanded,
            vertex_updates=self.last_vertex_updates,
            diagnostics={
                "open_queue_size": len(self.heap),
                "km": self.km,
                "g_start": float(self.g[self.start]),
                "rhs_start": float(self.rhs[self.start]),
            },
        )

    def _extract_path(self) -> Optional[list[tuple[int, int]]]:
        """Greedy descent following rhs-optimal successors (paper Sec. IV)."""
        path = [self._xy(self.start)]
        cur = self.start
        visited = {cur}
        while cur != self.goal:
            x, y = self._xy(cur)
            best_vid = -1
            best_val = INF
            # Deterministic tie-break: neighbours are emitted in fixed order.
            for nx, ny, _ in self.grid.neighbors(x, y):
                s = self._vid(nx, ny)
                c = self.grid.edge_cost((x, y), (nx, ny))
                val = c + self.g[s]
                if val < best_val - _EPS and np.isfinite(val):
                    best_val = val
                    best_vid = s
            if best_vid < 0 or not np.isfinite(best_val):
                return None
            cur = best_vid
            if cur in visited:
                return None  # defensive: should never happen on valid g
            visited.add(cur)
            path.append(self._xy(cur))
            if len(path) > self.size + 1:
                return None
        return path


# ---------------------------------------------------------------------- #
# Independent Dijkstra baseline (start -> goal, fresh heap every time)
# ---------------------------------------------------------------------- #
def _dijkstra(grid: Grid, start: tuple[int, int], goal: tuple[int, int]):
    """Fresh, stateless Dijkstra. Returns (distance_by_vertex, pop_count)."""
    if grid.is_blocked(*start) or grid.is_blocked(*goal):
        return {}, 0
    s0 = start[1] * grid.width + start[0]
    dist = {s0: 0.0}
    heap = [(0.0, 0, s0)]
    counter = 0
    pops = 0
    while heap:
        d, _, u = heapq.heappop(heap)
        if d > dist[u] + _EPS:
            continue
        pops += 1
        ux, uy = u % grid.width, u // grid.width
        for nx, ny, _ in grid.neighbors(ux, uy):
            c = grid.edge_cost((ux, uy), (nx, ny))
            if not np.isfinite(c):
                continue
            v = ny * grid.width + nx
            nd = d + c
            if nd + _EPS < dist.get(v, INF):
                dist[v] = nd
                counter += 1
                heapq.heappush(heap, (nd, counter, v))
    return dist, pops


def dijkstra_full(
    grid: Grid, start: tuple[int, int], goal: tuple[int, int]
) -> tuple[float, int]:
    """Return (optimal cost or inf, settled-vertex count) from scratch."""
    dist, pops = _dijkstra(grid, start, goal)
    g = goal[1] * grid.width + goal[0]
    return float(dist.get(g, INF)), pops


def dijkstra_cost(grid: Grid, start: tuple[int, int], goal: tuple[int, int]) -> float:
    """Optimal start->goal cost from a stateless Dijkstra (INF if unreachable)."""
    return dijkstra_full(grid, start, goal)[0]


def dijkstra_path(grid: Grid, start: tuple[int, int], goal: tuple[int, int]) -> Optional[list[tuple[int, int]]]:
    """Optimal start->goal path via fresh Dijkstra with predecessor tracking."""
    if grid.is_blocked(*start) or grid.is_blocked(*goal):
        return None
    s0 = start[1] * grid.width + start[0]
    g0 = goal[1] * grid.width + goal[0]
    dist = {s0: 0.0}
    prev: dict[int, int] = {}
    heap = [(0.0, 0, s0)]
    counter = 0
    while heap:
        d, _, u = heapq.heappop(heap)
        if d > dist[u] + _EPS:
            continue
        if u == g0:
            break
        ux, uy = u % grid.width, u // grid.width
        for nx, ny, _ in grid.neighbors(ux, uy):
            c = grid.edge_cost((ux, uy), (nx, ny))
            if not np.isfinite(c):
                continue
            v = ny * grid.width + nx
            nd = d + c
            if nd + _EPS < dist.get(v, INF):
                dist[v] = nd
                prev[v] = u
                counter += 1
                heapq.heappush(heap, (nd, counter, v))
    if g0 not in dist:
        return None
    path: list[tuple[int, int]] = []
    cur = g0
    while cur != s0:
        path.append((cur % grid.width, cur // grid.width))
        cur = prev[cur]
    path.append(start)
    path.reverse()
    return path
