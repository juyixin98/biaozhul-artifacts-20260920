"""Bipartite matching for the all-different constraint.

Given current domains, build the variable-value bipartite graph and compute
a maximum matching with Hopcroft-Karp. The matching is the backbone of
Régin's propagator: unmatched values and alternating-cycle structure tell us
which (variable, value) edges can never belong to a solution.
"""

from __future__ import annotations

import sys
from collections import deque
from collections.abc import Iterable, Mapping

# A matching maps variable -> matched value.
Matching = dict[str, int]

sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))


def hopcroft_karp(
    neighbors: Mapping[str, Iterable[int]],
) -> tuple[Matching, set[str]]:
    """Return a maximum-cardinality matching and the set of free variables.

    ``neighbors`` maps each left-side (variable) node to its adjacent right
    side (value) nodes. Classic Hopcroft-Karp BFS layering plus DFS
    augmentation.
    """
    adjacency = {var: list(values) for var, values in neighbors.items()}
    match_var: Matching = {}
    match_value: dict[int, str] = {}
    infinity = float("inf")
    dist: dict[str, float] = {}

    def bfs() -> bool:
        queue: deque[str] = deque()
        for var in adjacency:
            if var not in match_var:
                dist[var] = 0
                queue.append(var)
            else:
                dist[var] = infinity
        reachable_free_value = False
        while queue:
            var = queue.popleft()
            for value in adjacency[var]:
                owner = match_value.get(value)
                if owner is None:
                    reachable_free_value = True
                elif dist[owner] == infinity:
                    dist[owner] = dist[var] + 1
                    queue.append(owner)
        return reachable_free_value

    def dfs(var: str) -> bool:
        for value in adjacency[var]:
            owner = match_value.get(value)
            if owner is None or (
                dist[owner] == dist[var] + 1 and dfs(owner)
            ):
                match_var[var] = value
                match_value[value] = var
                return True
        dist[var] = infinity
        return False

    while bfs():
        for var in adjacency:
            if var not in match_var:
                dfs(var)

    free = {var for var in adjacency if var not in match_var}
    return match_var, free
