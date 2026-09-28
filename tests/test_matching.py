"""Hopcroft-Karp matching tests against an independent brute-force matcher."""

from __future__ import annotations

import random

from app.solver.matching import hopcroft_karp


def brute_force_max_matching(neighbors: dict[str, list[int]]) -> int:
    """Maximum matching by trying all partial injective assignments."""
    variables = list(neighbors)
    best = 0

    def search(index: int, used_values: set[int], size: int) -> None:
        nonlocal best
        if size + (len(variables) - index) <= best:
            return
        if index == len(variables):
            best = max(best, size)
            return
        search(index + 1, used_values, size)
        for value in neighbors[variables[index]]:
            if value not in used_values:
                used_values.add(value)
                search(index + 1, used_values, size + 1)
                used_values.remove(value)

    search(0, set(), 0)
    return best


def assert_matching_valid(matching: dict[str, int], neighbors) -> None:
    assert len(set(matching.values())) == len(matching), "values not injective"
    for variable, value in matching.items():
        assert value in neighbors[variable]


def test_perfect_matching_case() -> None:
    neighbors = {"a": [1, 2], "b": [2, 3], "c": [1, 3]}
    matching, free = hopcroft_karp(neighbors)
    assert_matching_valid(matching, neighbors)
    assert len(matching) == 3
    assert free == set()


def test_no_perfect_matching_reports_hall_deficit() -> None:
    neighbors = {"a": [1], "b": [1], "c": [1, 2]}
    matching, free = hopcroft_karp(neighbors)
    assert_matching_valid(matching, neighbors)
    assert len(matching) == 2
    assert len(free) == 1


def test_matches_brute_force_on_random_graphs() -> None:
    rng = random.Random(20260928)
    for case in range(500):
        size = rng.randint(1, 7)
        value_pool = list(range(rng.randint(1, 7)))
        neighbors = {
            f"x{i}": rng.sample(value_pool, rng.randint(0, len(value_pool)))
            for i in range(size)
        }
        matching, free = hopcroft_karp(neighbors)
        assert_matching_valid(matching, neighbors)
        assert len(matching) == brute_force_max_matching(neighbors)
        assert free == {v for v in neighbors if v not in matching}


def test_empty_neighbors_handled() -> None:
    matching, free = hopcroft_karp({"a": [], "b": [1]})
    assert matching == {"b": 1}
    assert free == {"a"}
