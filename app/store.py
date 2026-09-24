"""In-memory map sessions: grid + D* Lite state bound to a map snapshot.

Snapshot discipline
-------------------
A session has a monotonically increasing ``revision`` and a cryptographic
``snapshot_id`` (SHA-256 over the full map state). D* Lite's incremental
g/rhs state is valid only for the snapshot it was repaired under. Every
mutating call therefore:

1. requires the client to name the ``expected_snapshot_id`` it reasoned
   about -- a stale client gets HTTP 409 ``snapshot_mismatch`` instead of
   silently reusing a dead path;
2. applies the changes, recomputes the snapshot and feeds D* Lite exactly
   the changed edge set (state is *repaired*, never carried over blindly);
3. returns the new snapshot id.

Paths returned for revision N are only meaningful against snapshot N.
The goal is fixed for a session (the D* Lite precondition); changing the
goal means creating a new map.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Optional

import numpy as np

from . import crypto
from .planner import (
    DStarLite,
    Grid,
    dijkstra_full,
    dijkstra_path,
)

_COST_TOL = 1e-9


class NotFound(KeyError):
    """Unknown map id."""


class SnapshotMismatch(RuntimeError):
    """Client named a snapshot that is not the current revision."""


class InvalidMapState(ValueError):
    """Illegal cell/coordinate for this map."""


@dataclass
class PlanOutcome:
    reachable: bool
    cost: float
    path: Optional[list[tuple[int, int]]]
    path_cost_check: Optional[float]
    path_valid: Optional[bool]
    dijkstra_cost: float
    optimal_match: bool
    reexpanded_nodes: int
    vertex_updates: int
    dijkstra_settled_nodes: int
    open_queue_size: int
    km: float


class MapSession:
    def __init__(
        self,
        *,
        width: int,
        height: int,
        cost: Optional[np.ndarray],
        blocked: Optional[np.ndarray],
        start: tuple[int, int],
        goal: tuple[int, int],
        connectivity: int,
        diagonal_rule: str,
    ):
        self.map_id = uuid.uuid4().hex
        self.revision = 0
        self.lock = threading.RLock()
        self.goal = goal
        self.connectivity = connectivity
        self.diagonal_rule = diagonal_rule
        self.width, self.height = width, height

        if cost is None:
            cost = np.ones((height, width), dtype=np.float64)
        if blocked is None:
            blocked = np.zeros((height, width), dtype=bool)
        self.grid = Grid(cost, blocked, connectivity=connectivity, diagonal_rule=diagonal_rule)
        self._check_endpoint("start", start)
        self._check_endpoint("goal", goal)
        if start == goal:
            raise InvalidMapState("start and goal must differ")
        self.start = start
        self.hmac_key = crypto.new_hmac_key()
        self.snapshot_id = self._compute_snapshot()
        self.dstar = DStarLite(self.grid, start, goal)

    # ------------------------------------------------------------------ #
    def _check_endpoint(self, name: str, p: tuple[int, int]) -> None:
        x, y = p
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise InvalidMapState(f"{name} {p} out of bounds")
        if self.grid.is_blocked(x, y):
            raise InvalidMapState(f"{name} {p} is on a blocked cell")

    def _compute_snapshot(self) -> str:
        payload = crypto.canonical_snapshot_payload(
            width=self.width,
            height=self.height,
            cost=self.grid.cost,
            blocked=self.grid.blocked,
            connectivity=self.connectivity,
            diagonal_rule=self.diagonal_rule,
            start=self.start,
            goal=self.goal,
        )
        return crypto.snapshot_digest(payload)

    def _require_snapshot(self, expected: Optional[str]) -> None:
        if expected is not None and expected != self.snapshot_id:
            raise SnapshotMismatch(
                f"expected {expected} but map is at {self.snapshot_id} "
                f"(revision {self.revision})"
            )

    # ------------------------------------------------------------------ #
    def update_cells(self, expected: str, updates: list[dict]) -> list[tuple[int, int]]:
        with self.lock:
            self._require_snapshot(expected)
            changed: list[tuple[int, int]] = []
            for u in updates:
                x, y = u["x"], u["y"]
                if not (0 <= x < self.width and 0 <= y < self.height):
                    raise InvalidMapState(f"cell ({x},{y}) out of bounds")
                if (x, y) == self.goal:
                    raise InvalidMapState("the goal cell cannot be modified")
                # cost == null means "block this cell"; an explicit blocked
                # flag works too.
                will_block = bool(u.get("blocked")) or u.get("cost", 0) is None
                if (x, y) == self.start and will_block:
                    raise InvalidMapState(
                        "cannot block the current start cell; move start first"
                    )
                changed.append((x, y))
                if u.get("cost") is not None:
                    self.grid.cost[y, x] = float(u["cost"])
                if u.get("blocked") is not None:
                    self.grid.blocked[y, x] = bool(u["blocked"])
                if u.get("cost", 0) is None:
                    self.grid.blocked[y, x] = True
            # dedup while keeping order
            changed = list(dict.fromkeys(changed))
            if self.grid.is_blocked(*self.start):
                raise InvalidMapState("cannot block the current start cell; move start first")
            # refresh derived heuristic scale
            self.grid.min_weight = float(self.grid.cost.min()) if self.grid.cost.size else float("inf")
            self.dstar.edge_costs_changed(changed)
            self.revision += 1
            self.snapshot_id = self._compute_snapshot()
            return changed

    def move_start(self, expected: str, new_start: tuple[int, int]) -> None:
        with self.lock:
            self._require_snapshot(expected)
            self._check_endpoint("start", new_start)
            if new_start == self.goal:
                raise InvalidMapState("start cannot equal goal")
            if new_start != self.start:
                self.dstar.move_start(new_start)
                self.start = new_start
                self.revision += 1
                self.snapshot_id = self._compute_snapshot()

    # ------------------------------------------------------------------ #
    def plan(self, expected: Optional[str] = None) -> PlanOutcome:
        with self.lock:
            self._require_snapshot(expected)
            result = self.dstar.plan()
            dij_cost, dij_pops = dijkstra_full(self.grid, self.start, self.goal)
            reachable = result.reachable
            cost = result.cost if reachable else float("inf")
            optimal = (not reachable and not np.isfinite(dij_cost)) or (
                reachable
                and np.isfinite(dij_cost)
                and abs(cost - dij_cost) <= _COST_TOL * max(1.0, abs(dij_cost))
            )

            path = result.path
            path_cost_check: Optional[float] = None
            path_valid: Optional[bool] = None
            if reachable and path is not None:
                path_cost_check, path_valid = self._validate_path(path, cost)

            return PlanOutcome(
                reachable=reachable,
                cost=cost,
                path=path,
                path_cost_check=path_cost_check,
                path_valid=path_valid,
                dijkstra_cost=dij_cost,
                optimal_match=bool(optimal),
                reexpanded_nodes=result.reexpanded_nodes,
                vertex_updates=result.vertex_updates,
                dijkstra_settled_nodes=dij_pops,
                open_queue_size=result.diagnostics["open_queue_size"],
                km=result.diagnostics["km"],
            )

    def _validate_path(self, path: list[tuple[int, int]], claimed: float) -> tuple[float, bool]:
        """Walk the returned path on the current grid and sum true edge costs."""
        if path[0] != self.start or path[-1] != self.goal:
            return float("nan"), False
        total = 0.0
        seen = {path[0]}
        for a, b in zip(path, path[1:]):
            if b in seen:
                return total, False
            seen.add(b)
            c = self.grid.edge_cost(a, b)
            if not np.isfinite(c):
                return total, False
            total += c
        ok = abs(total - claimed) <= _COST_TOL * max(1.0, abs(claimed))
        return total, ok

    def baseline_path(self) -> Optional[list[tuple[int, int]]]:
        with self.lock:
            return dijkstra_path(self.grid, self.start, self.goal)


class MapStore:
    """Thread-safe process-local registry of sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, MapSession] = {}
        self._lock = threading.Lock()

    def create(self, **kwargs) -> MapSession:
        session = MapSession(**kwargs)
        with self._lock:
            self._sessions[session.map_id] = session
        return session

    def get(self, map_id: str) -> MapSession:
        session = self._sessions.get(map_id)
        if session is None:
            raise NotFound(map_id)
        return session

    def delete(self, map_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(map_id, None) is not None
