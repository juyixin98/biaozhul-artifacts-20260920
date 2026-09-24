"""Hybrid A* planner over discrete (x, y, theta) states.

The search lattice is continuous in space but states are keyed on a
discretized (ix, iy, itheta) cell so that at most one best node is kept
per cell. Edges are the motion primitives from `primitives.py`; every
edge is densely sampled and collision-checked before being accepted.
"""

from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass, field

import numpy as np

from .collision import CollisionChecker
from .grid_map import GridMap
from .heuristic import GridHeuristic
from .primitives import Primitive, build_primitives, simulate_primitive, wrap_angle
from .vehicle import Vehicle

TWO_PI = 2.0 * math.pi


@dataclass
class PlannerConfig:
    """Tunable parameters of the hybrid A* search."""

    heading_bins: int = 36          # discretization of theta
    primitive_length: float = 1.0   # arc length of one primitive (m)
    sample_step: float = 0.25       # collision-check sampling step (m)
    reverse_penalty: float = 2.0    # cost multiplier while reversing
    gear_switch_penalty: float = 2.0  # flat cost for changing gear
    curvature_penalty: float = 0.1  # cost per (|kappa| * length)
    use_heuristic: bool = True      # False -> zero-heuristic (Dijkstra) baseline
    heuristic_weight: float = 1.0   # 1.0 keeps the heuristic admissible
    allow_reverse: bool = True
    max_expansions: int = 200_000
    goal_tol_xy: float = 0.75       # goal position tolerance (m)
    goal_tol_theta: float = math.radians(20.0)


@dataclass
class PlanResult:
    """Outcome of one planning call."""

    success: bool
    path_x: list[float] = field(default_factory=list)
    path_y: list[float] = field(default_factory=list)
    path_theta: list[float] = field(default_factory=list)
    path_gear: list[int] = field(default_factory=list)
    cost: float = math.inf
    expansions: int = 0
    elapsed_ms: float = 0.0
    message: str = ""

    def path(self) -> list[dict]:
        return [
            {"x": x, "y": y, "theta": t, "gear": g}
            for x, y, t, g in zip(
                self.path_x, self.path_y, self.path_theta, self.path_gear
            )
        ]


class _Node:
    __slots__ = ("x", "y", "theta", "gear", "g", "parent", "prim", "samples")

    def __init__(self, x, y, theta, gear, g, parent, prim, samples):
        self.x = x
        self.y = y
        self.theta = theta
        self.gear = gear
        self.g = g
        self.parent = parent
        self.prim = prim
        self.samples = samples  # (xs, ys, thetas) dense rollout incl. start


class HybridAStarPlanner:
    """Plans collision-free, curvature-bounded paths on a GridMap."""

    def __init__(
        self,
        grid: GridMap,
        vehicle: Vehicle | None = None,
        config: PlannerConfig | None = None,
    ):
        self.grid = grid
        self.vehicle = vehicle or Vehicle()
        self.config = config or PlannerConfig()
        self.checker = CollisionChecker(grid, self.vehicle)
        self.primitives: list[Primitive] = build_primitives(
            self.vehicle.kappa_max,
            self.config.primitive_length,
            allow_reverse=self.config.allow_reverse,
        )

    # -- state discretization ------------------------------------------------

    def _key(self, x: float, y: float, theta: float) -> tuple[int, int, int]:
        cfg = self.config
        ix = int(round((x - self.grid.origin[0]) / self.grid.resolution))
        iy = int(round((y - self.grid.origin[1]) / self.grid.resolution))
        ith = int(round((theta % TWO_PI) / TWO_PI * cfg.heading_bins)) % cfg.heading_bins
        return ix, iy, ith

    # -- edge cost -----------------------------------------------------------

    def _edge_cost(self, prim: Primitive, parent_gear: int) -> float:
        cfg = self.config
        cost = prim.length
        if prim.gear < 0:
            cost *= cfg.reverse_penalty
        cost += cfg.curvature_penalty * abs(prim.kappa) * prim.length
        if prim.gear != parent_gear:
            cost += cfg.gear_switch_penalty
        return cost

    # -- main search ---------------------------------------------------------

    def plan(
        self,
        start: tuple[float, float, float],
        goal: tuple[float, float, float],
    ) -> PlanResult:
        t0 = time.perf_counter()
        cfg = self.config
        sx, sy, st = start
        gx, gy, gt = goal

        if not self.checker.is_pose_free(sx, sy, st):
            return self._fail(t0, "start pose is in collision")
        if not self.checker.is_pose_free(gx, gy, gt):
            return self._fail(t0, "goal pose is in collision")

        heuristic = (
            GridHeuristic(self.grid, (gx, gy), self.vehicle.circle_radius)
            if cfg.use_heuristic
            else None
        )

        def h(x: float, y: float) -> float:
            if heuristic is None:
                return 0.0
            # The search stops anywhere inside the goal tolerance disk, so
            # the raw cost-to-go to the goal *point* overestimates the
            # remaining cost by up to goal_tol_xy. Subtracting it keeps the
            # heuristic admissible with respect to the goal region.
            est = heuristic.cost_to_go(x, y) - cfg.goal_tol_xy
            return cfg.heuristic_weight * max(0.0, est)

        start_node = _Node(sx, sy, st, 1, 0.0, None, None, None)
        best_g: dict[tuple[int, int, int], float] = {self._key(sx, sy, st): 0.0}
        open_heap: list[tuple[float, float, int, _Node]] = []
        counter = 0
        heapq.heappush(open_heap, (h(sx, sy), 0.0, counter, start_node))
        expansions = 0
        goal_node: _Node | None = None

        while open_heap:
            _, g, _, node = heapq.heappop(open_heap)
            key = self._key(node.x, node.y, node.theta)
            if g > best_g.get(key, math.inf) + 1e-9:
                continue  # stale entry
            expansions += 1
            if expansions > cfg.max_expansions:
                return self._fail(
                    t0, f"expansion limit ({cfg.max_expansions}) reached", expansions
                )
            if (
                math.hypot(node.x - gx, node.y - gy) <= cfg.goal_tol_xy
                and abs(wrap_angle(node.theta - gt)) <= cfg.goal_tol_theta
            ):
                goal_node = node
                break
            for prim in self.primitives:
                xs, ys, ths = simulate_primitive(
                    node.x, node.y, node.theta, prim, cfg.sample_step
                )
                if not self.checker.is_path_free(xs[1:], ys[1:], ths[1:]):
                    continue
                nx, ny, nt = float(xs[-1]), float(ys[-1]), float(ths[-1])
                ng = g + self._edge_cost(prim, node.gear)
                nkey = self._key(nx, ny, nt)
                if ng < best_g.get(nkey, math.inf) - 1e-9:
                    best_g[nkey] = ng
                    child = _Node(nx, ny, nt, prim.gear, ng, node, prim, (xs, ys, ths))
                    counter += 1
                    heapq.heappush(open_heap, (ng + h(nx, ny), ng, counter, child))

        if goal_node is None:
            return self._fail(t0, "no path found (open list exhausted)", expansions)

        result = self._reconstruct(goal_node)
        result.expansions = expansions
        result.elapsed_ms = (time.perf_counter() - t0) * 1000.0
        result.message = "ok"
        return result

    # -- helpers -------------------------------------------------------------

    def _reconstruct(self, goal_node: _Node) -> PlanResult:
        segments: list[tuple[np.ndarray, np.ndarray, np.ndarray, int]] = []
        node = goal_node
        while node.parent is not None:
            xs, ys, ths = node.samples
            segments.append((xs, ys, ths, node.gear))
            node = node.parent
        segments.reverse()

        px: list[float] = []
        py: list[float] = []
        pt: list[float] = []
        pg: list[int] = []
        # Start pose with the gear of the first segment.
        first = segments[0]
        px.append(float(first[0][0]))
        py.append(float(first[1][0]))
        pt.append(float(first[2][0]))
        pg.append(first[3])
        for xs, ys, ths, gear in segments:
            for i in range(1, len(xs)):
                px.append(float(xs[i]))
                py.append(float(ys[i]))
                pt.append(float(ths[i]))
                pg.append(gear)
        return PlanResult(
            success=True,
            path_x=px,
            path_y=py,
            path_theta=pt,
            path_gear=pg,
            cost=goal_node.g,
        )

    @staticmethod
    def _fail(t0: float, message: str, expansions: int = 0) -> PlanResult:
        return PlanResult(
            success=False,
            expansions=expansions,
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            message=message,
        )
