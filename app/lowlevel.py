"""Low-level space-time A* for a single agent under CBS constraints.

Constraints:
- vertex constraint (cell, t): the agent may not occupy ``cell`` at time ``t``.
- edge constraint (u, v, t): the agent may not move u -> v between t and t+1.

An agent's cost is its arrival time at the goal. Once arrived it stays at the
goal forever (goal occupation), so the search may only terminate at the goal
at time t if no vertex constraint forbids the goal cell at any time >= t.
"""

from __future__ import annotations

import heapq
import math

from .grid import Coord, GridMap

VertexConstraint = tuple[Coord, int]          # (cell, t)
EdgeConstraint = tuple[Coord, Coord, int]     # (from, to, t)


def low_level_search(
    grid: GridMap,
    start: Coord,
    goal: Coord,
    vertex_constraints: set[VertexConstraint],
    edge_constraints: set[EdgeConstraint],
    max_time: int,
) -> list[Coord] | None:
    """Return a minimum-cost constraint-respecting path (positions per timestep,
    index 0 = start) or None if none exists within ``max_time``."""
    dist = grid.distances_to(goal)
    if math.isinf(dist[start]):
        return None
    if (start, 0) in vertex_constraints:
        return None

    # Latest time the goal cell is constrained; we may stop at the goal only
    # strictly after this, because the agent occupies the goal forever after.
    goal_blocked_until = max(
        (t for (cell, t) in vertex_constraints if cell == goal), default=-1
    )

    # Heap entries: (f, g, pos). best_g maps (pos, t) -> g for reopening.
    open_heap: list[tuple[float, int, Coord]] = [(dist[start], 0, start)]
    best_g: dict[tuple[Coord, int], int] = {(start, 0): 0}
    parent: dict[tuple[Coord, int], tuple[Coord, int]] = {}

    while open_heap:
        _, g, pos = heapq.heappop(open_heap)
        state = (pos, g)
        if best_g.get(state) != g:
            continue  # stale heap entry
        if pos == goal and g > goal_blocked_until:
            return _reconstruct(parent, state)
        if pos == goal:
            continue  # may not leave the goal once reached (goal occupation)
        if g >= max_time:
            continue
        for nxt in grid.neighbors(pos):
            t = g + 1
            if (nxt, t) in vertex_constraints:
                continue
            if (pos, nxt, t - 1) in edge_constraints:
                continue
            key = (nxt, t)
            if t < best_g.get(key, math.inf):
                best_g[key] = t
                parent[key] = state
                heapq.heappush(open_heap, (t + dist[nxt], t, nxt))
    return None


def _reconstruct(
    parent: dict[tuple[Coord, int], tuple[Coord, int]],
    state: tuple[Coord, int],
) -> list[Coord]:
    chain = [state]
    while chain[-1] in parent:
        chain.append(parent[chain[-1]])
    chain.reverse()
    return [pos for pos, _ in chain]
