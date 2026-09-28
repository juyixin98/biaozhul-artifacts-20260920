"""Independent reference oracle used ONLY by tests.

This module deliberately re-implements exhaustive state-space search from
scratch instead of importing :mod:`strips_planner.core.search`. It shares only
the *data* classes (:class:`Problem`, :class:`GroundAction`, :class:`Atom`)
with the system under test; transition semantics, queues and parent maps are
all written independently here. Tests use it to cross-check optimality,
unsolvability and heuristic admissibility - the expected answers are never
produced by the planner under test.
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass

from strips_planner.models import GroundAction, Problem

# Independent transition semantics (intentionally duplicated, not imported).


def _applicable(action: GroundAction, state: frozenset) -> bool:
    for lit in action.pre_pos:
        if lit not in state:
            return False
    for lit in action.pre_neg:
        if lit in state:
            return False
    return True


def _apply(action: GroundAction, state: frozenset) -> frozenset:
    removed = {lit for lit in state if lit not in action.delete}
    return frozenset(removed | set(action.add))


def _start(problem: Problem) -> frozenset:
    return frozenset(problem.init)


def _is_goal(problem: Problem, state: frozenset) -> bool:
    return problem.goal_pos <= state and not (problem.goal_neg & state)


@dataclass(frozen=True, slots=True)
class OracleResult:
    reachable: int
    solvable: bool
    min_length: int | None
    min_cost: float | None
    length_plan: tuple[str, ...]
    cost_plan: tuple[str, ...]


def exhaustive_solve(problem: Problem) -> OracleResult:
    """Return ground truth: shortest-length and cheapest plans via raw BFS/Dijkstra."""
    start = _start(problem)
    shortest_len, length_plan = _bfs_length(problem, start)
    cheapest, cost_plan = _dijkstra_cost(problem, start)
    reachable = _count_reachable(problem, start)
    return OracleResult(
        reachable=reachable,
        solvable=shortest_len is not None,
        min_length=shortest_len,
        min_cost=cheapest,
        length_plan=length_plan,
        cost_plan=cost_plan,
    )


def _bfs_length(problem: Problem, start: frozenset) -> tuple[int | None, tuple[str, ...]]:
    if _is_goal(problem, start):
        return 0, ()
    parents = {start: (None, None)}
    queue: deque[frozenset] = deque([start])
    while queue:
        state = queue.popleft()
        for action in problem.ground_actions:
            if not _applicable(action, state):
                continue
            child = _apply(action, state)
            if child in parents:
                continue
            parents[child] = (state, action.label)
            if _is_goal(problem, child):
                return _reconstruct(child, parents)
            queue.append(child)
    return None, ()


def _dijkstra_cost(problem: Problem, start: frozenset) -> tuple[float | None, tuple[str, ...]]:
    if _is_goal(problem, start):
        return 0.0, ()
    best = {start: 0.0}
    parents = {start: (None, None)}
    heap = [(0.0, 0, start)]
    tie = 1
    while heap:
        g, _, state = heapq.heappop(heap)
        if g > best[state] + 1e-12:
            continue
        if _is_goal(problem, state):
            labels = _reconstruct_labels(state, parents)
            return g, labels
        for action in problem.ground_actions:
            if not _applicable(action, state):
                continue
            child = _apply(action, state)
            ng = g + action.cost
            if ng + 1e-12 < best.get(child, float("inf")):
                best[child] = ng
                parents[child] = (state, action.label)
                heapq.heappush(heap, (ng, tie, child))
                tie += 1
    return None, ()


def _count_reachable(problem: Problem, start: frozenset) -> int:
    seen = {start}
    queue = deque([start])
    while queue:
        state = queue.popleft()
        for action in problem.ground_actions:
            if not _applicable(action, state):
                continue
            child = _apply(action, state)
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return len(seen)


def optimal_cost_to_all_goals(problem: Problem) -> dict[frozenset, float]:
    """Cheapest cost at which each reachable state is reached (Dijkstra labels)."""
    start = _start(problem)
    best = {start: 0.0}
    heap = [(0.0, 0, start)]
    tie = 1
    while heap:
        g, _, state = heapq.heappop(heap)
        if g > best[state] + 1e-12:
            continue
        for action in problem.ground_actions:
            if not _applicable(action, state):
                continue
            child = _apply(action, state)
            ng = g + action.cost
            if ng + 1e-12 < best.get(child, float("inf")):
                best[child] = ng
                heapq.heappush(heap, (ng, tie, child))
                tie += 1
    return best


def goal_distances(problem: Problem) -> dict[frozenset, float]:
    """True h*: cheapest cost from every reachable state to any goal state.

    Enumerate the reachable graph from the initial state, reverse every action
    edge (keeping its cost), then run Dijkstra from goal-satisfying states.
    States from which the goal is unreachable are absent from the result.
    """
    start = _start(problem)
    seen = {start}
    queue: deque[frozenset] = deque([start])
    reverse: dict[frozenset, list[tuple[frozenset, float]]] = {}
    while queue:
        state = queue.popleft()
        for action in problem.ground_actions:
            if not _applicable(action, state):
                continue
            child = _apply(action, state)
            reverse.setdefault(child, []).append((state, action.cost))
            if child not in seen:
                seen.add(child)
                queue.append(child)

    goals = {s for s in seen if _is_goal(problem, s)}
    dist = {g: 0.0 for g in goals}
    heap = [(0.0, i, g) for i, g in enumerate(goals)]
    heapq.heapify(heap)
    tie = len(heap)
    while heap:
        d, _, state = heapq.heappop(heap)
        if d > dist[state] + 1e-12:
            continue
        for predecessor, cost in reverse.get(state, ()):
            nd = d + cost
            if nd + 1e-12 < dist.get(predecessor, float("inf")):
                dist[predecessor] = nd
                heapq.heappush(heap, (nd, tie, predecessor))
                tie += 1
    return dist


def _reconstruct(goal: frozenset, parents: dict) -> tuple[int, tuple[str, ...]]:
    labels = _reconstruct_labels(goal, parents)
    return len(labels), labels


def _reconstruct_labels(goal: frozenset, parents: dict) -> tuple[str, ...]:
    labels: list[str] = []
    state = goal
    while True:
        parent, label = parents[state]
        if parent is None:
            break
        labels.append(label)
        state = parent
    labels.reverse()
    return tuple(labels)
