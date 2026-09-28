"""Independent reference oracle for entity-resolution clustering.

IMPORTANT: this is deliberately a *separate, brute-force implementation*. It
does not import or reuse :mod:`entity_resolution.clustering`; the test-suite
uses it to cross-check the production solver. If both shared code, agreement
would prove nothing.

Strategy: enumerate **every** set partition of the records (Bell number),
discard partitions that violate must/cannot links, and score the remainder with
an explicit disagreement objective. The set of attaining partitions is
returned (not just one) so tests can assert both the value and, where useful,
uniqueness.
"""

from __future__ import annotations

from itertools import combinations
from typing import Callable, Iterable

Pair = tuple[str, str]


def all_partitions(items: list[str]) -> Iterable[tuple[frozenset[str], ...]]:
    """Enumerate set partitions by the "next element joins or starts" recursion.

    Written independently of the production restricted-growth enumerator.
    """
    result: list[tuple[frozenset[str], ...]] = []

    def grow(remaining: list[str], groups: list[set[str]]) -> None:
        if not remaining:
            result.append(tuple(frozenset(g) for g in groups))
            return
        head, *tail = remaining
        # Join each existing group.
        for i in range(len(groups)):
            branched = [set(g) for g in groups]
            branched[i].add(head)
            grow(tail, branched)
        # Start a new group.
        grow(tail, [*groups, {head}])

    grow(items, [])
    return result


def _canonical(partition: Iterable[Iterable[str]]) -> frozenset[frozenset[str]]:
    return frozenset(frozenset(b) for b in partition)


def reference_optimum(
    records: list[str],
    weights: dict[Pair, float],
    must: Iterable[Pair] = (),
    cannot: Iterable[Pair] = (),
) -> dict:
    """Return the exhaustive optimum.

    Returns ``{"feasible": [...], "optimal": [...], "cost": float}`` where
    ``feasible`` lists every constraint-valid partition and ``optimal`` the
    subset attaining the minimum correlation-clustering cost.
    """
    must = {tuple(sorted(p)) for p in must}  # type: ignore[assignment]
    cannot = {tuple(sorted(p)) for p in cannot}  # type: ignore[assignment]

    feasible: list[frozenset[frozenset[str]]] = []
    best_cost = float("inf")
    optimal: list[frozenset[frozenset[str]]] = []

    for partition in all_partitions(records):
        block_of: dict[str, int] = {}
        for idx, block in enumerate(partition):
            for member in block:
                block_of[member] = idx
        if any(block_of[a] != block_of[b] for a, b in must):
            continue
        if any(block_of[a] == block_of[b] for a, b in cannot):
            continue
        feasible.append(partition)

        cost = 0.0
        for a, b in combinations(sorted(records), 2):
            w = weights.get((a, b), 0.0)
            if block_of[a] == block_of[b]:
                cost += 1.0 - w
            else:
                cost += w
        if cost < best_cost:
            best_cost = cost
            optimal = [partition]
        elif cost == best_cost:
            optimal.append(partition)

    return {
        "feasible": [_canonical(p) for p in feasible],
        "optimal": [_canonical(p) for p in optimal],
        "cost": best_cost,
    }


def threshold_connected_components(
    records: list[str], weights: dict[Pair, float], threshold: float
) -> frozenset[frozenset[str]]:
    """The *naive* baseline: union every pair at/above a threshold.

    This is transitive by construction and is used only to prove the system
    returns a different (constraint/objective-correct) answer.
    """
    parent = {r: r for r in records}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for (a, b), w in weights.items():
        if w >= threshold:
            union(a, b)

    groups: dict[str, set[str]] = {}
    for r in records:
        groups.setdefault(find(r), set()).add(r)
    return _canonical(groups.values())


def is_feasible(
    partition: Iterable[Iterable[str]],
    must: Iterable[Pair],
    cannot: Iterable[Pair],
) -> bool:
    blocks = list(partition)
    block_of = {m: i for i, b in enumerate(blocks) for m in b}
    return all(block_of[a] == block_of[b] for a, b in must) and all(
        block_of[a] != block_of[b] for a, b in cannot
    )


def partition_cost(
    partition: Iterable[Iterable[str]],
    records: list[str],
    weights: dict[Pair, float],
) -> float:
    """Explicit cost used to score the production answer independently."""
    blocks = list(partition)
    block_of = {m: i for i, b in enumerate(blocks) for m in b}
    cost = 0.0
    for a, b in combinations(sorted(records), 2):
        w = weights.get((a, b), 0.0)
        cost += (1.0 - w) if block_of[a] == block_of[b] else w
    return cost
