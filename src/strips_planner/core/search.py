"""Search kernel: BFS, uniform-cost, A* and greedy best-first over STRIPS.

Optimality contract
-------------------
* ``bfs``     - minimum number of *actions* (unit steps), regardless of costs.
* ``ucs``     - minimum sum of action costs (A* with h = 0).
* ``astar``   - minimum sum of action costs iff the heuristic is admissible.
                ``hmax`` is; ``goalcount`` is not and is rejected for A*.
* ``greedy``  - no optimality promise; ``optimal`` is always reported false.

Strictly positive action costs are required and enforced at validation time.

Search never dresses a guess as a plan. Exactly one of three outcomes returns:

* :class:`PlanFound` via ``status=FOUND``      - plan, then independently
  re-checked by :class:`~strips_planner.core.executor.PlanExecutor`.
* ``status=UNSOLVABLE`` - reachable state space exhausted; goal provably
  unreachable.
* ``status=LIMIT``      - a bound (expanded states / depth / seconds) was hit
  first; solvability is *unknown* and the bound is reported.

State identity is the bitmask from :class:`StateEncoder`, so equal states
reached on different paths dedupe to one node and can be reopened on a cheaper
path.
"""

from __future__ import annotations

import heapq
import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from strips_planner.core.heuristics import Heuristic, make_heuristic
from strips_planner.core.state import (
    State,
    StateEncoder,
    applicable,
    apply_action,
    goal_satisfied,
    initial_state,
)
from strips_planner.errors import ComputationError
from strips_planner.models import Problem


class SearchStatus(str, Enum):
    FOUND = "FOUND"
    UNSOLVABLE = "UNSOLVABLE"
    LIMIT = "LIMIT"


@dataclass(frozen=True, slots=True)
class SearchLimits:
    max_expanded: int = 100_000
    max_depth: int = 1_000
    max_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    status: SearchStatus
    algorithm: str
    heuristic: str
    optimal: bool
    plan: tuple[str, ...]
    path_cost: float
    expanded: int
    generated: int
    reopened: int
    depth: int
    limit: str | None
    limit_value: float | None
    frontier_size: int
    elapsed_seconds: float
    reason: str
    event_log: tuple[dict[str, Any], ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "algorithm": self.algorithm,
            "heuristic": self.heuristic,
            "optimal": self.optimal,
            "plan": list(self.plan),
            "path_cost": None if math.isinf(self.path_cost) else self.path_cost,
            "expanded": self.expanded,
            "generated": self.generated,
            "reopened": self.reopened,
            "depth": self.depth,
            "limit": self.limit,
            "limit_value": self.limit_value,
            "frontier_size": self.frontier_size,
            "elapsed_seconds": self.elapsed_seconds,
            "reason": self.reason,
            "event_log": list(self.event_log),
        }


_SUPPORTED = {"bfs", "ucs", "astar", "greedy"}


def run_search(
    problem: Problem,
    *,
    algorithm: str = "astar",
    heuristic: str = "hmax",
    limits: SearchLimits | None = None,
    capture_events: bool = False,
    max_events: int = 500,
) -> SearchOutcome:
    if algorithm not in _SUPPORTED:
        raise ComputationError(
            f"unsupported algorithm {algorithm!r}; expected one of {sorted(_SUPPORTED)}",
            code="UNSUPPORTED_ALGORITHM",
        )
    limits = limits or SearchLimits()
    if limits.max_expanded < 1 or limits.max_depth < 0 or limits.max_seconds <= 0:
        raise ComputationError("search limits must be positive", code="INVALID_LIMIT")

    chosen_heuristic = "zero" if algorithm in ("ucs", "bfs") else heuristic
    if algorithm == "greedy":
        chosen_heuristic = heuristic
    h_func, h_admissible = make_heuristic(chosen_heuristic, problem)
    if algorithm == "astar" and not h_admissible:
        raise ComputationError(
            f"heuristic {chosen_heuristic!r} is not admissible; the A* optimality "
            "promise would be false. Use 'hmax' or run 'greedy'.",
            code="NON_ADMISSIBLE_HEURISTIC",
        )

    engine = _Engine(problem, chosen_heuristic, h_func, limits,
                      capture_events, max_events)
    if algorithm == "bfs":
        return engine.breadth_first()
    if algorithm == "greedy":
        return engine.best_first(algorithm="greedy", use_cost=False, optimal=False,
                                 heuristic_label=chosen_heuristic)
    if algorithm == "ucs":
        return engine.best_first(algorithm="ucs", use_cost=True, optimal=True,
                                 heuristic_label="zero")
    return engine.best_first(algorithm="astar", use_cost=True, optimal=True,
                             heuristic_label=chosen_heuristic)


@dataclass(slots=True)
class _Parent:
    parent: int | None
    action: str | None
    g: float
    depth: int


class _Engine:
    def __init__(
        self,
        problem: Problem,
        heuristic_name: str,
        heuristic: Heuristic,
        limits: SearchLimits,
        capture_events: bool,
        max_events: int,
    ) -> None:
        self.problem = problem
        self.heuristic_name = heuristic_name
        self.h = heuristic
        self.limits = limits
        self.capture_events = capture_events
        self.max_events = max_events
        self.encoder = StateEncoder(problem)
        self.start = initial_state(problem)
        self.events: list[dict[str, Any]] = []
        self.expanded = 0
        self.generated = 0
        self.reopened = 0
        self.deepest = 0
        self._tie = 0

    # -- BFS (fewest actions) ---------------------------------------------

    def breadth_first(self) -> SearchOutcome:
        clock = time.perf_counter()
        start_code = self.encoder.encode(self.start)
        if goal_satisfied(self.problem, self.start):
            return self._found(
                start_code, {start_code: _Parent(None, None, 0.0, 0)},
                algorithm="bfs", heuristic_label="none(unit-steps)",
                optimal=True, depth=0, frontier_size=0, clock=clock,
                reason="initial state satisfies goal",
            )

        parents: dict[int, _Parent] = {start_code: _Parent(None, None, 0.0, 0)}
        frontier: deque[tuple[State, int]] = deque([(self.start, start_code)])
        deadline = clock + self.limits.max_seconds

        while frontier:
            if self.expanded >= self.limits.max_expanded:
                return self._limit("max_expanded", self.limits.max_expanded,
                                   len(frontier), "bfs", "none(unit-steps)", clock)
            if time.perf_counter() >= deadline:
                return self._limit("max_seconds", self.limits.max_seconds,
                                   len(frontier), "bfs", "none(unit-steps)", clock)

            state, code = frontier.popleft()
            node = parents[code]
            if node.depth >= self.limits.max_depth:
                # BFS frontier is depth-ordered: everything queued behind this
                # node is at least as deep, so no in-bound expansion remains.
                return self._limit("max_depth", self.limits.max_depth,
                                   len(frontier), "bfs", "none(unit-steps)", clock)

            self.expanded += 1
            self._record_event(state, node.g, node.depth, math.inf)
            for action in self.problem.ground_actions:
                if not applicable(action, state):
                    continue
                child = apply_action(action, state)
                child_code = self.encoder.encode(child)
                self.generated += 1
                if child_code in parents:
                    continue
                child_depth = node.depth + 1
                self.deepest = max(self.deepest, child_depth)
                parents[child_code] = _Parent(code, action.label,
                                              node.g + action.cost, child_depth)
                frontier.append((child, child_code))
                if goal_satisfied(self.problem, child):
                    return self._found(
                        child_code, parents, algorithm="bfs",
                        heuristic_label="none(unit-steps)", optimal=True,
                        depth=child_depth, frontier_size=len(frontier), clock=clock,
                        reason="goal generated at minimum action depth",
                    )

        return self._unsolvable("bfs", "none(unit-steps)", clock,
                                "reachable state space exhausted without reaching the goal")

    # -- UCS / A* / greedy (priority queue) -------------------------------

    def best_first(self, *, algorithm: str, use_cost: bool, optimal: bool,
                   heuristic_label: str) -> SearchOutcome:
        clock = time.perf_counter()
        deadline = clock + self.limits.max_seconds
        start_code = self.encoder.encode(self.start)
        parents: dict[int, _Parent] = {start_code: _Parent(None, None, 0.0, 0)}
        # Heap entries: (priority, g, tiebreak, state, code). g is carried so a
        # stale entry is detectable even after a node has been reopened.
        h0 = self._heuristic_value(self.start)
        heap: list[tuple[float, float, int, State, int]] = [
            (h0, 0.0, self._tie, self.start, start_code)
        ]
        depth_cutoff = False

        while heap:
            if self.expanded >= self.limits.max_expanded:
                return self._limit("max_expanded", self.limits.max_expanded,
                                   len(heap), algorithm, heuristic_label, clock)
            if time.perf_counter() >= deadline:
                return self._limit("max_seconds", self.limits.max_seconds, len(heap),
                                   algorithm, heuristic_label, clock)

            priority, g_entry, _, state, code = heapq.heappop(heap)
            node = parents[code]
            if g_entry + 1e-12 < node.g:
                continue  # stale: node was reopened on a cheaper path
            if goal_satisfied(self.problem, state):
                return self._found(
                    code, parents, algorithm=algorithm, heuristic_label=heuristic_label,
                    optimal=optimal, depth=node.depth, frontier_size=len(heap),
                    clock=clock,
                    reason="goal popped with minimal key"
                    + (" (proven minimum cost)" if optimal else " (no optimality guarantee)"),
                )
            if node.depth >= self.limits.max_depth:
                depth_cutoff = True
                continue

            self.expanded += 1
            self._record_event(state, node.g, node.depth, priority)
            for action in self.problem.ground_actions:
                if not applicable(action, state):
                    continue
                child = apply_action(action, state)
                child_code = self.encoder.encode(child)
                self.generated += 1
                g_new = node.g + action.cost
                known = parents.get(child_code)
                if known is None:
                    depth_new = node.depth + 1
                    self.deepest = max(self.deepest, depth_new)
                    parents[child_code] = _Parent(code, action.label, g_new, depth_new)
                    self._push(heap, child, child_code, g_new, use_cost)
                elif g_new + 1e-9 < known.g:
                    # Cheaper path to an already seen (possibly expanded) node:
                    # reopen. With strictly positive costs this is the only
                    # relaxation an admissible search needs for correctness.
                    known.parent = code
                    known.action = action.label
                    known.g = g_new
                    known.depth = node.depth + 1
                    self.reopened += 1
                    self._push(heap, child, child_code, g_new, use_cost)

        if depth_cutoff:
            return self._limit("max_depth", self.limits.max_depth, 0,
                               algorithm, heuristic_label, clock)
        reason = ("priority queue exhausted: all reachable states explored, "
                  "the goal is unreachable")
        return self._unsolvable(algorithm, heuristic_label, clock, reason)

    def _push(self, heap, child: State, child_code: int, g_new: float,
              use_cost: bool) -> None:
        h_child = self._heuristic_value(child)
        priority = (g_new + h_child) if use_cost else h_child
        self._tie += 1
        heapq.heappush(heap, (priority, g_new, self._tie, child, child_code))

    def _heuristic_value(self, state: State) -> float:
        value = float(self.h(self.problem, state))
        if value < 0 or math.isnan(value):
            raise ComputationError(
                f"heuristic returned invalid value {value}",
                code="BAD_HEURISTIC_VALUE",
            )
        return value

    # -- outcome construction ---------------------------------------------

    def _found(self, goal_code, parents, *, algorithm, heuristic_label, optimal,
               depth, frontier_size, clock, reason) -> SearchOutcome:
        labels, path_cost = self._reconstruct(goal_code, parents)
        return SearchOutcome(
            status=SearchStatus.FOUND,
            algorithm=algorithm,
            heuristic=heuristic_label,
            optimal=optimal,
            plan=tuple(labels),
            path_cost=round(path_cost, 12),
            expanded=self.expanded,
            generated=self.generated,
            reopened=self.reopened,
            depth=depth,
            limit=None,
            limit_value=None,
            frontier_size=frontier_size,
            elapsed_seconds=time.perf_counter() - clock,
            reason=reason,
            event_log=tuple(self.events),
        )

    def _limit(self, bound, value, frontier_size, algorithm, heuristic_label, clock):
        return SearchOutcome(
            status=SearchStatus.LIMIT,
            algorithm=algorithm,
            heuristic=heuristic_label,
            optimal=False,
            plan=(),
            path_cost=math.inf,
            expanded=self.expanded,
            generated=self.generated,
            reopened=self.reopened,
            depth=min(self.deepest, self.limits.max_depth),
            limit=bound,
            limit_value=float(value),
            frontier_size=frontier_size,
            elapsed_seconds=time.perf_counter() - clock,
            reason=(f"search stopped at bound {bound}={value}; "
                    "solvability beyond this bound is UNKNOWN"),
            event_log=tuple(self.events),
        )

    def _unsolvable(self, algorithm, heuristic_label, clock, reason):
        return SearchOutcome(
            status=SearchStatus.UNSOLVABLE,
            algorithm=algorithm,
            heuristic=heuristic_label,
            optimal=True,
            plan=(),
            path_cost=math.inf,
            expanded=self.expanded,
            generated=self.generated,
            reopened=self.reopened,
            depth=self.deepest,
            limit=None,
            limit_value=None,
            frontier_size=0,
            elapsed_seconds=time.perf_counter() - clock,
            reason=reason,
            event_log=tuple(self.events),
        )

    def _reconstruct(self, goal_code: int, parents: dict[int, _Parent]) -> tuple[list[str], float]:
        labels: list[str] = []
        cur = goal_code
        node = parents[cur]
        while node.parent is not None:
            labels.append(node.action or "")
            cur = node.parent
            node = parents[cur]
        labels.reverse()
        return labels, parents[goal_code].g

    def _record_event(self, state: State, g: float, depth: int, f: float) -> None:
        if not self.capture_events or len(self.events) >= self.max_events:
            return
        self.events.append({
            "seq": self.expanded,
            "event": "expand",
            "depth": depth,
            "g": round(g, 12),
            "f": None if math.isinf(f) else round(f, 12),
            "state": self.encoder.render(state),
        })
