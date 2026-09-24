"""Conflict-Based Search (CBS) for multi-agent path finding on a grid.

Semantics (per the problem statement):
- 4-connected grid, actions = move to a free neighbour cell or wait in place.
- Vertex conflict: two agents occupy the same cell at the same timestep.
- Edge conflict: two agents swap cells between consecutive timesteps.
- After reaching its goal an agent keeps occupying the goal cell forever.
- Objective: minimise the sum of individual arrival times (sum of costs).

CBS is complete and optimal for this problem (given a sufficient time bound);
the returned constraint-tree statistics describe the high-level search.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field

from .grid import Coord, GridMap
from .lowlevel import EdgeConstraint, VertexConstraint, low_level_search


@dataclass(frozen=True)
class VertexConflict:
    a1: int
    a2: int
    cell: Coord
    t: int


@dataclass(frozen=True)
class EdgeConflict:
    a1: int
    a2: int
    u: Coord  # a1 moves u -> v
    v: Coord
    t: int  # between t and t+1


Conflict = VertexConflict | EdgeConflict


@dataclass
class CBSStats:
    high_level_nodes: int = 0       # constraint-tree nodes generated
    high_level_expanded: int = 0    # nodes popped from OPEN
    conflicts_detected: int = 0     # first-conflict checks that found one
    low_level_calls: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "high_level_nodes": self.high_level_nodes,
            "high_level_expanded": self.high_level_expanded,
            "conflicts_detected": self.conflicts_detected,
            "low_level_calls": self.low_level_calls,
        }


@dataclass
class CBSSolution:
    paths: list[list[Coord]]  # paths[i][t] = position of agent i at time t
    cost: int                 # sum of arrival times
    stats: CBSStats

    @property
    def makespan(self) -> int:
        return max(len(p) - 1 for p in self.paths)


@dataclass
class _Node:
    constraints_v: list[set[VertexConstraint]]
    constraints_e: list[set[EdgeConstraint]]
    paths: list[list[Coord]]
    cost: int


def position_at(path: list[Coord], t: int) -> Coord:
    """Position at time t; the agent stays at its goal after arrival."""
    return path[min(t, len(path) - 1)]


def detect_first_conflict(paths: list[list[Coord]]) -> Conflict | None:
    """Deterministically find the earliest conflict (vertex before edge)."""
    max_t = max(len(p) for p in paths)
    n = len(paths)
    for t in range(max_t):
        # vertex conflicts at time t
        seen: dict[Coord, int] = {}
        for i in range(n):
            c = position_at(paths[i], t)
            if c in seen:
                return VertexConflict(seen[c], i, c, t)
            seen[c] = i
        # edge conflicts between t and t+1
        for i in range(n):
            ui, vi = position_at(paths[i], t), position_at(paths[i], t + 1)
            if ui == vi:
                continue
            for j in range(i + 1, n):
                uj, vj = position_at(paths[j], t), position_at(paths[j], t + 1)
                if ui == vj and vi == uj:
                    return EdgeConflict(i, j, ui, vi, t)
    return None


def _default_max_time(grid: GridMap, n_agents: int) -> int:
    # Generous bound for small offline instances: enough room for every agent
    # to traverse all free cells and wait for every other agent to do the same.
    return max(1, (n_agents + 1) * len(grid.free_cells))


def solve_cbs(
    grid: GridMap,
    starts: list[Coord],
    goals: list[Coord],
    max_time: int | None = None,
    max_high_level_nodes: int = 100_000,
) -> CBSSolution | None:
    """Solve one MAPF instance with CBS. Returns None if unsolvable."""
    n = len(starts)
    if n == 0:
        return CBSSolution(paths=[], cost=0, stats=CBSStats())
    if max_time is None:
        max_time = _default_max_time(grid, n)

    stats = CBSStats()

    def plan(agent: int, cv: set[VertexConstraint], ce: set[EdgeConstraint]):
        stats.low_level_calls += 1
        return low_level_search(grid, starts[agent], goals[agent], cv, ce, max_time)

    root_cv: list[set[VertexConstraint]] = [set() for _ in range(n)]
    root_ce: list[set[EdgeConstraint]] = [set() for _ in range(n)]
    root_paths: list[list[Coord]] = []
    for i in range(n):
        p = plan(i, root_cv[i], root_ce[i])
        if p is None:
            return None  # an agent cannot even reach its goal alone
        root_paths.append(p)

    counter = itertools.count()
    open_heap: list[tuple[int, int, _Node]] = []
    root = _Node(root_cv, root_ce, root_paths, sum(len(p) - 1 for p in root_paths))
    stats.high_level_nodes += 1
    heapq.heappush(open_heap, (root.cost, next(counter), root))

    while open_heap:
        cost, _, node = heapq.heappop(open_heap)
        stats.high_level_expanded += 1
        conflict = detect_first_conflict(node.paths)
        if conflict is None:
            return CBSSolution(paths=node.paths, cost=cost, stats=stats)
        stats.conflicts_detected += 1

        for agent, new_v, new_e in _branch_constraints(conflict):
            cv = [set(s) for s in node.constraints_v]
            ce = [set(s) for s in node.constraints_e]
            cv[agent].update(new_v)
            ce[agent].update(new_e)
            path = plan(agent, cv[agent], ce[agent])
            if path is None:
                continue  # this branch is infeasible
            paths = list(node.paths)
            paths[agent] = path
            child = _Node(cv, ce, paths, sum(len(p) - 1 for p in paths))
            stats.high_level_nodes += 1
            if stats.high_level_nodes > max_high_level_nodes:
                return None  # give up rather than run forever
            heapq.heappush(open_heap, (child.cost, next(counter), child))
    return None


def _branch_constraints(
    conflict: Conflict,
) -> list[tuple[int, set[VertexConstraint], set[EdgeConstraint]]]:
    """One child per conflicting agent, each with one added constraint."""
    if isinstance(conflict, VertexConflict):
        return [
            (conflict.a1, {(conflict.cell, conflict.t)}, set()),
            (conflict.a2, {(conflict.cell, conflict.t)}, set()),
        ]
    return [
        (conflict.a1, set(), {(conflict.u, conflict.v, conflict.t)}),
        (conflict.a2, set(), {(conflict.v, conflict.u, conflict.t)}),
    ]


def validate_solution(
    grid: GridMap,
    starts: list[Coord],
    goals: list[Coord],
    paths: list[list[Coord]],
) -> list[str]:
    """Replay a joint timetable and return a list of violations (empty = valid).

    Checks: start/goal positions, legal moves, no vertex conflicts,
    no edge (swap) conflicts, and goal occupation after arrival.
    """
    errors: list[str] = []
    if len(paths) != len(starts):
        return [f"expected {len(starts)} paths, got {len(paths)}"]
    for i, path in enumerate(paths):
        if not path:
            errors.append(f"agent {i}: empty path")
            continue
        if path[0] != starts[i]:
            errors.append(f"agent {i}: starts at {path[0]}, expected {starts[i]}")
        if path[-1] != goals[i]:
            errors.append(f"agent {i}: ends at {path[-1]}, expected {goals[i]}")
        for c in path:
            if not grid.is_free(c):
                errors.append(f"agent {i}: occupies blocked/out-of-bounds cell {c}")
        for t in range(len(path) - 1):
            if path[t + 1] not in grid.neighbors(path[t]):
                errors.append(
                    f"agent {i}: illegal move {path[t]} -> {path[t + 1]} at t={t}"
                )
        # once at the goal the agent must stay there
        if goals[i] in path:
            first = path.index(goals[i])
            for t in range(first, len(path)):
                if path[t] != goals[i]:
                    errors.append(f"agent {i}: left its goal at t={t}")
                    break
    conflict = detect_first_conflict(paths)
    if conflict is not None:
        errors.append(f"unresolved conflict: {conflict}")
    return errors
