"""Exhaustive joint-state search used to verify CBS optimality on tiny maps.

Dijkstra over the joint state (one cell per agent). The cost of a solution is
the sum of arrival times; since sum_i arrival_i = sum_{t>=1} (#agents not yet
at their goal at t-1), each joint transition from a state costs the number of
agents not yet at their goal in that state. Agents at their goal must wait
(goal occupation), matching the CBS semantics.
"""

from __future__ import annotations

import heapq
import itertools

from .grid import Coord, GridMap


def brute_force_optimal_cost(
    grid: GridMap,
    starts: list[Coord],
    goals: list[Coord],
    max_expansions: int = 2_000_000,
) -> int | None:
    """Minimum sum of arrival times, or None if unsolvable / budget exceeded."""
    n = len(starts)
    if n == 0:
        return 0
    start_state = tuple(starts)
    goal_state = tuple(goals)

    # Per-agent move options depend only on the cell, so precompute.
    options: dict[Coord, list[Coord]] = {}

    def moves_for(agent: int, pos: Coord) -> list[Coord]:
        if pos == goals[agent]:
            return [pos]  # goal occupation: never leave
        if pos not in options:
            options[pos] = grid.neighbors(pos)
        return options[pos]

    dist = {start_state: 0}
    heap: list[tuple[int, tuple[Coord, ...]]] = [(0, start_state)]
    expansions = 0
    while heap:
        d, state = heapq.heappop(heap)
        if d != dist[state]:
            continue
        if state == goal_state:
            return d
        expansions += 1
        if expansions > max_expansions:
            return None
        weight = sum(1 for i in range(n) if state[i] != goals[i])
        per_agent = [moves_for(i, state[i]) for i in range(n)]
        for combo in itertools.product(*per_agent):
            if not _joint_move_valid(state, combo):
                continue
            nd = d + weight
            if nd < dist.get(combo, 1 << 60):
                dist[combo] = nd
                heapq.heappush(heap, (nd, combo))
    return None


def _joint_move_valid(state: tuple[Coord, ...], combo: tuple[Coord, ...]) -> bool:
    if len(set(combo)) != len(combo):
        return False  # vertex conflict
    for i in range(len(state)):
        for j in range(i + 1, len(state)):
            if combo[i] == state[j] and combo[j] == state[i]:
                return False  # edge (swap) conflict
    return True
