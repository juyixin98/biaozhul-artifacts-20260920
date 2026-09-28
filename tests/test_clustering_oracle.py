"""Exhaustive cross-check against the INDEPENDENT oracle.

These are the core correctness guarantees:

* the production exact solver returns a partition the brute-force oracle marks
  optimal and feasible (agreement across two separately written algorithms),
* a similarity chain A~B~C with weak/forbidden A~C is NOT collapsed the way a
  threshold connected-components baseline would collapse it,
* the large-instance path is a distinguishable RESOURCE_EXHAUSTED failure.
"""

from __future__ import annotations

import itertools

import pytest

from entity_resolution.clustering import (
    SolverConfig,
    _bell_number,
    solve,
)
from entity_resolution.errors import ResourceExhaustedError

from .oracle import (
    is_feasible,
    partition_cost,
    reference_optimum,
    threshold_connected_components,
)


def _as_set(clusters):
    return frozenset(frozenset(c) for c in clusters)


def test_bell_numbers_match_known_values() -> None:
    # Bell numbers B0..B7 (sanity for the budget short-circuit).
    assert [_bell_number(n, 10**9) for n in range(8)] == [
        1, 1, 2, 5, 15, 52, 203, 877
    ]


@pytest.mark.parametrize("seed", range(40))
def test_exact_solver_matches_independent_oracle(seed: int) -> None:
    # Deterministic pseudo-random small problems, n in 3..5 (B5 = 52).
    n = 3 + seed % 3
    records = [f"r{i}" for i in range(n)]
    weights = {}
    state = seed * 2654435761 & 0xFFFFFFFF or 1
    for a, b in itertools.combinations(records, 2):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        weights[(a, b)] = round(((state >> 8) % 1000) / 1000.0, 3)

    result = solve(records, weights, config=SolverConfig(mode="exact"))
    answer = _as_set(result.clusters)

    oracle = reference_optimum(records, weights)
    assert result.optimal is True
    assert answer in set(oracle["optimal"])
    # Independently re-score the production answer and compare the value.
    assert abs(partition_cost(result.clusters, records, weights) - oracle["cost"]) < 1e-9


def test_chain_with_forbidden_endpoints_is_split() -> None:
    # A~B and B~C strong, A~C explicitly forbidden.
    records = ["A", "B", "C"]
    weights = {("A", "B"): 0.9, ("B", "C"): 0.9, ("A", "C"): 0.05}
    must: list[tuple[str, str]] = []
    cannot = [("A", "C")]

    result = solve(
        records, weights, must=must, cannot=cannot,
        config=SolverConfig(mode="exact"),
    )
    answer = _as_set(result.clusters)

    # Feasible and globally optimal per the independent oracle.
    assert is_feasible(result.clusters, must, cannot)
    oracle = reference_optimum(records, weights, must, cannot)
    assert answer in set(oracle["optimal"])

    # The forbidden endpoints are never together.
    for a, b in cannot:
        assert not any({a, b} <= block for block in result.clusters)

    # The naive baseline WOULD merge all three (the bug we must not have).
    naive = threshold_connected_components(records, weights, threshold=0.5)
    assert naive == frozenset({frozenset({"A", "B", "C"})})
    assert answer != naive


def test_chain_without_constraint_global_optimum_breaks_transitivity() -> None:
    # No cannot-link at all: a strong path with a very weak closing edge must
    # still not merge all three, because that is what minimizes the objective.
    records = ["A", "B", "C"]
    weights = {("A", "B"): 0.95, ("B", "C"): 0.95, ("A", "C"): 0.01}
    result = solve(records, weights, config=SolverConfig(mode="exact"))
    oracle = reference_optimum(records, weights)
    answer = _as_set(result.clusters)
    assert answer in set(oracle["optimal"])
    # Optimum here keeps A,B together and C apart (NOT all three).
    assert answer != frozenset({frozenset({"A", "B", "C"})})
    assert frozenset({"A", "B"}) in answer


def test_must_link_forces_merge_despite_weak_similarity() -> None:
    records = ["A", "B"]
    weights = {("A", "B"): 0.0}
    result = solve(records, weights, must=[("A", "B")],
                   config=SolverConfig(mode="exact"))
    assert _as_set(result.clusters) == frozenset({frozenset({"A", "B"})})


def test_two_separate_locks_never_merge_even_with_strong_edge() -> None:
    records = ["A", "B"]
    weights = {("A", "B"): 1.0}
    result = solve(
        records, weights,
        locked_blocks=[frozenset({"A"}), frozenset({"B"})],
        config=SolverConfig(mode="exact"),
    )
    assert _as_set(result.clusters) == frozenset({frozenset({"A"}), frozenset({"B"})})


def test_exact_budget_exhaustion_is_distinct_error() -> None:
    # 9 singleton blocks => B9 = 21147 > 1000 budget.
    records = [f"r{i}" for i in range(9)]
    with pytest.raises(ResourceExhaustedError) as exc:
        solve(records, {}, config=SolverConfig(mode="exact", max_exact_partitions=1000))
    assert exc.value.category == "RESOURCE_EXHAUSTED"
    assert exc.value.details["blocks"] == 9


def test_auto_mode_falls_back_to_greedy_for_large_input() -> None:
    records = [f"r{i}" for i in range(9)]
    weights = {
        (a, b): 0.99 for a, b in
        [("r0", "r1"), ("r1", "r2"), ("r3", "r4")]
    }
    result = solve(
        records, weights,
        config=SolverConfig(mode="auto", max_exact_partitions=50),
    )
    assert result.method == "greedy"
    assert result.optimal is False
    answer = _as_set(result.clusters)
    # Direct strong edges merge, but r2 is NOT pulled into {r0,r1}: the
    # missing r0-r2 edge makes the 3-way merge costly (non-transitivity holds
    # on the large-input path too).
    assert frozenset({"r0", "r1"}) in answer
    assert frozenset({"r2"}) in answer
    assert frozenset({"r3", "r4"}) in answer
