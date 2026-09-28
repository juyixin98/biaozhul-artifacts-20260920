"""All-different global constraint: Régin's matching-based filtering.

Given variables X = {x1..xn} with finite domains, build the bipartite value
graph and compute a maximum matching M.

- If |M| < n the constraint is infeasible; the set of variables reachable
  from unmatched variables via alternating paths forms a Hall set S with
  |neighbours(S)| < |S|, returned as an explicit certificate.
- Otherwise every edge (x, v) that belongs to NO maximum matching is pruned:
  an edge is kept iff it is in M, lies on an alternating cycle (same SCC of
  the alternating digraph), or lies on an alternating path from a free value.

This subsumes (and is strictly stronger than) pairwise removal of assigned
values: Hall-set reasoning prunes values consumed by not-yet-assigned
variables as well.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .matching import maximum_matching


@dataclass(frozen=True)
class HallViolation:
    vars: list[str]
    values: list[int]


@dataclass
class AllDifferentResult:
    consistent: bool
    matching: dict[str, int] = field(default_factory=dict)
    removals: list[tuple[str, int]] = field(default_factory=list)
    hall: HallViolation | None = None


def _alternating_digraph(
    variables: list[str], adjacency: dict[str, list[int]], matching: dict[str, int]
) -> dict[object, list[object]]:
    """Matched edges point value->var, unmatched edges var->value."""
    graph: dict[object, list[object]] = {}
    matched_value = {v: x for x, v in matching.items()}
    for x in variables:
        graph.setdefault(x, [])
        for v in adjacency[x]:
            graph.setdefault(v, [])
            if matching.get(x) == v:
                graph[v].append(x)
            else:
                graph[x].append(v)
    return graph


def _reachable(graph: dict[object, list[object]], sources: list[object]) -> set[object]:
    seen = set(sources)
    stack = list(sources)
    while stack:
        node = stack.pop()
        for nxt in graph.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _strongly_connected_components(
    graph: dict[object, list[object]]
) -> dict[object, int]:
    """Iterative Kosaraju; returns node -> component id."""
    # Iterative Tarjan (single pass, no recursion limit issues).
    index_of: dict[object, int] = {}
    lowlink: dict[object, int] = {}
    on_stack: set[object] = set()
    stack: list[object] = []
    component: dict[object, int] = {}
    counter = 0
    n_components = 0

    for root in graph:
        if root in index_of:
            continue
        work = [(root, iter(graph[root]))]
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index_of:
                    index_of[nxt] = lowlink[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(graph.get(nxt, []))))
                    advanced = True
                    break
                if nxt in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component[member] = n_components
                    if member == node:
                        break
                n_components += 1
    return component


def filter_all_different(
    variables: list[str], domains: dict[str, frozenset[int]]
) -> AllDifferentResult:
    adjacency = {x: sorted(domains[x]) for x in variables}
    matching = maximum_matching(variables, adjacency)

    if len(matching) < len(variables):
        # Hall certificate: alternating reachability from unmatched variables.
        free_vars = [x for x in variables if x not in matching]
        graph = _alternating_digraph(variables, adjacency, matching)
        reached = _reachable(graph, free_vars)
        hall_vars = sorted(x for x in reached if isinstance(x, str))
        hall_values = sorted(v for v in reached if isinstance(v, int))
        return AllDifferentResult(
            consistent=False,
            matching=matching,
            hall=HallViolation(vars=hall_vars, values=hall_values),
        )

    graph = _alternating_digraph(variables, adjacency, matching)
    matched_values = set(matching.values())
    all_values = {v for x in variables for v in adjacency[x]}
    free_values = sorted(all_values - matched_values)
    reachable = _reachable(graph, free_values)
    component = _strongly_connected_components(graph)

    removals: list[tuple[str, int]] = []
    for x in variables:
        for v in adjacency[x]:
            if matching[x] == v:
                continue
            if component.get(x) == component.get(v):
                continue
            if x in reachable:
                continue
            removals.append((x, v))
    return AllDifferentResult(consistent=True, matching=matching, removals=removals)
