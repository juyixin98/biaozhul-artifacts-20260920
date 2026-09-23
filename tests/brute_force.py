"""Brute-force minimum-cost flow reference for small graphs.

Enumerates *every* flow vector that respects edge capacities, checks flow
conservation at all internal nodes, and picks the one with maximum total
s-t flow and, among those, minimum cost. Pure Python; only used by tests.
"""

from __future__ import annotations

from itertools import product
from typing import Optional


def brute_force_mcf(
    n: int,
    source: int,
    sink: int,
    edges: list[tuple[int, int, int, int]],
    max_flow_limit: Optional[int] = None,
) -> tuple[int, int]:
    """Return (max_flow, min_cost) by exhaustive enumeration."""
    m = len(edges)
    best_flow = -1
    best_cost: Optional[int] = None

    caps = [e[2] for e in edges]
    for flows in product(*(range(c + 1) for c in caps)):
        divergence = [0] * n
        cost = 0
        for f, (u, v, _cap, c) in enumerate(edges):
            x = flows[f]
            divergence[u] -= x
            divergence[v] += x
            cost += x * c
        for node in range(n):
            if node == source or node == sink:
                continue
            if divergence[node] != 0:
                break
        else:
            total = divergence[sink]  # net inflow at sink
            if total < 0 or divergence[source] != -total:
                continue
            if max_flow_limit is not None and total > max_flow_limit:
                continue
            if total > best_flow or (total == best_flow and
                                     (best_cost is None or cost < best_cost)):
                best_flow = total
                best_cost = cost

    assert best_flow >= 0 and best_cost is not None
    return best_flow, best_cost


def greedy_forward_only(
    n: int,
    source: int,
    sink: int,
    edges: list[tuple[int, int, int, int]],
) -> tuple[int, int]:
    """Naive greedy: repeatedly take the cheapest path using *forward* arcs
    only (no residual reverse arcs). Gets stuck prematurely when an optimal
    flow requires cancelling previously sent flow. Test helper.
    """
    import heapq

    residual = [e[2] for e in edges]
    total_flow = 0
    total_cost = 0

    while True:
        dist = [10**18] * n
        parent = [-1] * n
        dist[source] = 0
        heap = [(0, source)]
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist[u]:
                continue
            for i, (eu, ev, _cap, ec) in enumerate(edges):
                if eu == u and residual[i] > 0 and d + ec < dist[ev]:
                    dist[ev] = d + ec
                    parent[ev] = i
                    heapq.heappush(heap, (d + ec, ev))
        if parent[sink] < 0:
            break
        path = []
        x = sink
        while x != source:
            i = parent[x]
            path.append(i)
            x = edges[i][0]
        add = min(residual[i] for i in path)
        for i in path:
            residual[i] -= add
        total_flow += add
        total_cost += add * sum(edges[i][3] for i in path)

    return total_flow, total_cost
