"""Shared pytest fixtures/helpers."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.planner import DStarLite, Grid, dijkstra_full  # noqa: E402


@pytest.fixture
def rng():
    return np.random.default_rng(20260923)


def make_random_grid(rng, max_side=9, p_block=0.22, connectivity=None):
    w = int(rng.integers(4, max_side + 1))
    h = int(rng.integers(4, max_side + 1))
    cost = rng.random((h, w)) * 9.0 + 1.0
    blocked = rng.random((h, w)) < p_block
    blocked[0, 0] = False
    blocked[h - 1, w - 1] = False
    conn = connectivity if connectivity is not None else int(rng.choice([4, 8]))
    return Grid(cost, blocked, connectivity=conn)


def assert_optimal(planner: DStarLite, grid: Grid, start, goal, tol=1e-7):
    """Plan incrementally and compare against independent Dijkstra."""
    result = planner.plan()
    dc, _ = dijkstra_full(grid, start, goal)
    assert result.reachable == bool(np.isfinite(dc)), (
        f"reachability mismatch: d*={result.reachable} dijkstra={dc}"
    )
    if result.reachable:
        assert abs(result.cost - dc) <= tol * max(1.0, abs(dc)), (
            f"cost mismatch: d*={result.cost} dijkstra={dc}"
        )
        assert result.path is not None
        assert result.path[0] == start
        assert result.path[-1] == goal
        total = 0.0
        for a, b in zip(result.path, result.path[1:]):
            c = grid.edge_cost(a, b)
            assert np.isfinite(c), f"path uses impassable edge {a}->{b}"
            total += c
        assert abs(total - result.cost) <= 1e-6, (
            f"returned path sums to {total}, claimed {result.cost}"
        )
    else:
        assert result.path is None
    return result
