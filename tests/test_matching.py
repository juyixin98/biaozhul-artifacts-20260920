"""Hungarian algorithm correctness against brute force on random matrices."""
from __future__ import annotations

import math
import random

import pytest

from app.matching import brute_force_best, hungarian


def matrix_cost(cost, assignment):
    total = 0.0
    for r, c in enumerate(assignment):
        if c == -1:
            continue
        total += cost[r][c]
    return total


@pytest.mark.parametrize("seed", range(60))
def test_hungarian_matches_bruteforce(seed):
    rng = random.Random(seed)
    n = rng.randint(1, 5)
    m = rng.randint(1, 5)
    cost = [
        [rng.randint(0, 50) + rng.random() for _ in range(m)]
        for _ in range(n)
    ]
    total, assignment = hungarian(cost)

    # validity: distinct columns
    chosen = [c for c in assignment if c != -1]
    assert len(chosen) == len(set(chosen))
    assert all(0 <= c < m for c in chosen)
    assert matrix_cost(cost, assignment) == pytest.approx(total)
    assert total == pytest.approx(brute_force_best(cost))


def test_infinite_entries_force_idle():
    # Only the expensive cell is feasible for row 0.
    cost = [[math.inf, 100.0], [5.0, math.inf]]
    total, assignment = hungarian(cost)
    assert assignment == [1, 0]
    assert total == pytest.approx(105.0)


def test_all_infinite_leaves_everyone_idle():
    cost = [[math.inf, math.inf], [math.inf, math.inf]]
    total, assignment = hungarian(cost)
    assert assignment == [-1, -1]
    assert total == 0.0


def test_rectangular_more_robots_than_tasks():
    cost = [[9.0], [1.0], [4.0]]
    total, assignment = hungarian(cost)
    # cheapest row (row 1) wins the single task
    assert assignment[1] == 0
    assert assignment[0] == -1 and assignment[2] == -1
    assert total == pytest.approx(1.0)


def test_rectangular_more_tasks_than_robots():
    cost = [[9.0, 1.0, 4.0]]
    total, assignment = hungarian(cost)
    assert assignment == [1]
    assert total == pytest.approx(1.0)


def test_empty():
    assert hungarian([]) == (0.0, [])
