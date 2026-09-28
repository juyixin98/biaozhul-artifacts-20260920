"""Fill-reducing elimination orderings.

A permutation ``perm`` is returned, where ``perm[k]`` is the *original*
node eliminated at position ``k``. The numerical layer applies it as
P A P^T and applies the same permutation to the right-hand side.

Three orderings are provided:

* ``natural``        - identity (baseline for fill reporting);
* ``minimum_degree`` - greedy minimum-degree on the elimination graph
  (clique-update / MMD-style heuristic);
* ``nested_dissection`` - BFS-level vertex separator, recursively, with
  minimum degree used below a size cutoff.

These operate purely on the graph (sets of neighbours); the input matrix
is never densified.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class OrderingResult:
    perm: np.ndarray          # perm[k] = original node at position k
    inv_perm: np.ndarray      # inv_perm[orig] = position
    method: str
    fill_edges_added: int     # edges added during min-degree simulation


def graph_from_upper(rows: np.ndarray, cols: np.ndarray,
                     n: int) -> list[set[int]]:
    """Build symmetric adjacency sets (excluding self loops) from upper
    triangle COO edges."""
    adj: list[set[int]] = [set() for _ in range(n)]
    for r, c in zip(rows.tolist(), cols.tolist()):
        if r == c:
            continue
        lo, hi = (r, c) if r < c else (c, r)
        adj[lo].add(hi)
        adj[hi].add(lo)
    return adj


def natural_order(n: int) -> OrderingResult:
    perm = np.arange(n, dtype=np.int64)
    return OrderingResult(perm=perm, inv_perm=perm.copy(),
                          method="natural", fill_edges_added=0)


def _min_degree_core(adj0: list[set[int]], nodes: list[int]):
    """Greedy minimum degree on the induced graph of ``nodes``.

    Returns (local_order, fill_added). Eliminating v makes a clique of
    its remaining neighbours; we update adjacency sets explicitly.
    """
    node_set = set(nodes)
    adj = {v: set(adj0[v]) & node_set for v in nodes}
    # Live degrees, with lazy deletion.
    order: list[int] = []
    fill_added = 0
    remaining = set(nodes)
    while remaining:
        v = min(remaining, key=lambda u: (len(adj[u]), u))
        nbrs = adj[v] & remaining
        nbr_list = list(nbrs)
        # Clique completion among neighbours.
        for a_i in range(len(nbr_list)):
            for b_i in range(a_i + 1, len(nbr_list)):
                a, b = nbr_list[a_i], nbr_list[b_i]
                if b not in adj[a]:
                    adj[a].add(b)
                    adj[b].add(a)
                    fill_added += 1
        remaining.remove(v)
        order.append(v)
        for u in nbr_list:
            adj[u].discard(v)
    return order, fill_added


def minimum_degree_order(adj: list[set[int]]) -> OrderingResult:
    n = len(adj)
    order, fill_added = _min_degree_core(adj, list(range(n)))
    perm = np.asarray(order, dtype=np.int64)
    inv = np.empty(n, dtype=np.int64)
    inv[perm] = np.arange(n)
    return OrderingResult(perm=perm, inv_perm=inv,
                          method="minimum_degree",
                          fill_edges_added=fill_added)


def _bfs_layers(adj, source: int, allowed: set[int]) -> list[list[int]]:
    """BFS returning one list of nodes per distance level."""
    seen = {source: 0}
    frontier = [source]
    levels = [[source]]
    while frontier:
        nxt: list[int] = []
        for v in frontier:
            for u in adj[v]:
                if u in allowed and u not in seen:
                    seen[u] = 0
                    nxt.append(u)
        if not nxt:
            break
        levels.append(nxt)
        frontier = nxt
    return levels


def _farthest(adj, source: int, allowed: set[int]) -> int:
    seen = {source}
    frontier = deque([source])
    last = source
    while frontier:
        v = frontier.popleft()
        last = v
        for u in adj[v]:
            if u in allowed and u not in seen:
                seen.add(u)
                frontier.append(u)
    return last


def _components(allowed: set[int], adj) -> list[list[int]]:
    """Connected components of the induced subgraph on ``allowed``."""
    seen: set[int] = set()
    comps: list[list[int]] = []
    for start in allowed:
        if start in seen:
            continue
        comp: list[int] = []
        stack = [start]
        seen.add(start)
        while stack:
            v = stack.pop()
            comp.append(v)
            for u in adj[v]:
                if u in allowed and u not in seen:
                    seen.add(u)
                    stack.append(u)
        comps.append(comp)
    return comps


_ND_CUTOFF = 40


def _nested_dissection(adj, nodes: list[int]) -> list[int]:
    if len(nodes) <= _ND_CUTOFF:
        order, _ = _min_degree_core(adj, nodes)
        return order

    allowed = set(nodes)
    # Pseudo-diameter: two sweeps of BFS.
    endpoint_a = _farthest(adj, next(iter(allowed)), allowed)
    endpoint_b = _farthest(adj, endpoint_a, allowed)
    levels = _bfs_layers(adj, endpoint_b, allowed)
    if len(levels) < 3:
        order, _ = _min_degree_core(adj, nodes)
        return order

    # A level near the BFS diameter midpoint acts as vertex separator.
    sep = set(levels[len(levels) // 2])
    comps = _components(allowed - sep, adj)
    result: list[int] = []
    for comp in comps:
        result.extend(_nested_dissection(adj, comp))
    # Separator nodes are eliminated last; order them by minimum degree
    # locally to avoid a pathological tie order.
    sep_order, _ = _min_degree_core(adj, list(sep))
    result.extend(sep_order)
    return result


def nested_dissection_order(adj: list[set[int]]) -> OrderingResult:
    n = len(adj)
    order = _nested_dissection(adj, list(range(n)))
    perm = np.asarray(order, dtype=np.int64)
    inv = np.empty(n, dtype=np.int64)
    inv[perm] = np.arange(n)
    return OrderingResult(perm=perm, inv_perm=inv,
                          method="nested_dissection",
                          fill_edges_added=0)


ORDERINGS = {
    "natural": natural_order,
    "minimum_degree": minimum_degree_order,
    "nested_dissection": nested_dissection_order,
}


def compute_ordering(method: str, rows: np.ndarray, cols: np.ndarray,
                     n: int) -> OrderingResult:
    if method not in ORDERINGS:
        raise ValueError(f"unknown ordering {method!r}; "
                         f"choose from {sorted(ORDERINGS)}")
    if method == "natural":
        return natural_order(n)
    adj = graph_from_upper(rows, cols, n)
    return ORDERINGS[method](adj)
