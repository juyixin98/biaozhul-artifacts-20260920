"""D* Lite correctness tests against an independent Dijkstra baseline.

Scenarios mandated by the specification:
* obstacle addition / removal,
* unreachable goals,
* moving the start,
* diagonal corner-cut prohibition,
* negative-cost rejection,
* randomized fuzz sequences mixing everything.
"""

import numpy as np
import pytest

from app.planner import (
    CostValidationError,
    DStarLite,
    Grid,
    dijkstra_cost,
    dijkstra_full,
    dijkstra_path,
)

from conftest import assert_optimal, make_random_grid


# ---------------------------------------------------------------------- #
# Basic optimality
# ---------------------------------------------------------------------- #
def test_open_grid_optimal(rng):
    grid = make_random_grid(rng, p_block=0.0)
    planner = DStarLite(grid, (0, 0), (grid.width - 1, grid.height - 1))
    assert_optimal(planner, grid, (0, 0), (grid.width - 1, grid.height - 1))


def test_weighted_grid_optimal(rng):
    for _ in range(25):
        grid = make_random_grid(rng)
        planner = DStarLite(grid, (0, 0), (grid.width - 1, grid.height - 1))
        assert_optimal(planner, grid, (0, 0), (grid.width - 1, grid.height - 1))


def test_connectivity_four_optimal(rng):
    for _ in range(25):
        grid = make_random_grid(rng, connectivity=4)
        planner = DStarLite(grid, (0, 0), (grid.width - 1, grid.height - 1))
        assert_optimal(planner, grid, (0, 0), (grid.width - 1, grid.height - 1))


# ---------------------------------------------------------------------- #
# Validation rules
# ---------------------------------------------------------------------- #
def test_negative_costs_rejected():
    with pytest.raises(CostValidationError):
        Grid(-np.ones((3, 3)), np.zeros((3, 3), bool), connectivity=8)
    grid = Grid(np.ones((3, 3)), np.zeros((3, 3), bool), connectivity=8)
    with pytest.raises(CostValidationError):
        grid.update(np.array([[1.0, 1.0, -1.0]] * 3), None)


def test_nan_inf_costs_rejected():
    with pytest.raises(CostValidationError):
        Grid(np.full((2, 2), np.inf), np.zeros((2, 2), bool), 8)
    with pytest.raises(CostValidationError):
        Grid(np.full((2, 2), np.nan), np.zeros((2, 2), bool), 8)


def test_zero_weight_map():
    grid = Grid(np.zeros((6, 6)), np.zeros((6, 6), bool), 8)
    planner = DStarLite(grid, (0, 0), (5, 5))
    result = assert_optimal(planner, grid, (0, 0), (5, 5))
    assert result.cost == 0.0


def test_blocked_endpoints_rejected():
    blocked = np.zeros((3, 3), bool)
    blocked[0, 0] = True
    grid = Grid(np.ones((3, 3)), blocked, 8)
    with pytest.raises(CostValidationError):
        DStarLite(grid, (0, 0), (2, 2))
    blocked[0, 0] = False
    blocked[2, 2] = True
    grid = Grid(np.ones((3, 3)), blocked, 8)
    with pytest.raises(CostValidationError):
        DStarLite(grid, (0, 0), (2, 2))


# ---------------------------------------------------------------------- #
# Diagonal corner rule
# ---------------------------------------------------------------------- #
def test_diagonal_requires_both_orthogonals_open():
    grid = Grid(np.ones((3, 3)), np.zeros((3, 3), bool), 8)
    # one side blocked -> sliding allowed
    grid.blocked[0, 1] = True
    assert np.isfinite(grid.edge_cost((0, 0), (1, 1)))
    # both sides blocked -> cutting the corner forbidden
    grid.blocked[1, 0] = True
    assert grid.edge_cost((0, 0), (1, 1)) == float("inf")


def test_no_diagonal_in_four_connectivity():
    grid = Grid(np.ones((3, 3)), np.zeros((3, 3), bool), 4)
    assert grid.edge_cost((0, 0), (1, 1)) == float("inf")
    assert np.isfinite(grid.edge_cost((0, 0), (1, 0)))


def test_corner_rule_forces_detour():
    # 4x4 with the two cells guarding the natural diagonal blocked.
    blocked = np.zeros((4, 4), bool)
    blocked[0, 1] = blocked[1, 0] = True
    # start is then isolated -> unreachable; both algorithms must agree.
    grid = Grid(np.ones((4, 4)), blocked, 8)
    planner = DStarLite(grid, (0, 0), (3, 3))
    assert_optimal(planner, grid, (0, 0), (3, 3))

    # A wall with an actual way around: path must not cut the corner.
    blocked = np.zeros((5, 5), bool)
    blocked[1, 1] = blocked[1, 2] = True
    grid = Grid(np.ones((5, 5)), blocked, 8)
    planner = DStarLite(grid, (0, 0), (4, 4))
    result = assert_optimal(planner, grid, (0, 0), (4, 4))
    # Every diagonal step in the returned path must obey the corner rule.
    for a, b in zip(result.path, result.path[1:]):
        assert np.isfinite(grid.edge_cost(a, b))


# ---------------------------------------------------------------------- #
# Incremental: obstacle add/remove, unreachability, start moves
# ---------------------------------------------------------------------- #
def _grid_with_wall(width=9, height=5):
    cost = np.ones((height, width))
    blocked = np.zeros((height, width), bool)
    blocked[1:4, 4] = True  # vertical wall with gaps at top/bottom
    return Grid(cost, blocked, 8)


def test_obstacle_addition_makes_goal_unreachable():
    grid = _grid_with_wall()
    start, goal = (0, 2), (8, 2)
    planner = DStarLite(grid, start, goal)
    first = assert_optimal(planner, grid, start, goal)
    assert first.reachable
    # seal both gaps -> disconnected
    changed = [(4, 0), (4, 4)]
    grid.blocked[0, 4] = True
    grid.blocked[4, 4] = True
    planner.edge_costs_changed(changed)
    second = assert_optimal(planner, grid, start, goal)
    assert not second.reachable
    assert second.cost == float("inf")
    assert second.path is None


def test_obstacle_removal_repairs_path():
    grid = _grid_with_wall()
    start, goal = (0, 2), (8, 2)
    # start fully sealed
    grid.blocked[:, 4] = True
    planner = DStarLite(grid, start, goal)
    sealed = assert_optimal(planner, grid, start, goal)
    assert not sealed.reachable
    # reopen one gap: incremental repair must find the optimal route
    grid.blocked[2, 4] = False
    planner.edge_costs_changed([(4, 2)])
    reopened = assert_optimal(planner, grid, start, goal)
    assert reopened.reachable


def test_cost_update_changes_optimum():
    width = height = 7
    grid = Grid(np.ones((height, width)), np.zeros((height, width), bool), 8)
    start, goal = (0, 0), (6, 6)
    planner = DStarLite(grid, start, goal)
    before = assert_optimal(planner, grid, start, goal)
    # make the diagonal corridor cells cheap -> must keep optimum
    for x, y in [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)]:
        grid.cost[y, x] = 0.1
    planner.edge_costs_changed([(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])
    grid.min_weight = 0.1
    after = assert_optimal(planner, grid, start, goal)
    assert after.cost < before.cost


def test_move_start_far_and_near():
    grid = make_random_grid(np.random.default_rng(1), p_block=0.15)
    goal = (grid.width - 1, grid.height - 1)
    candidates = [
        (0, 0),
        (grid.width - 1, 0),
        (0, grid.height - 1),
        (grid.width // 2, grid.height // 2),
    ]
    starts = [p for p in candidates if not grid.is_blocked(*p) and p != goal]
    planner = DStarLite(grid, starts[0], goal)
    start = starts[0]
    for new_start in starts[1:] + starts[:1]:
        planner.move_start(new_start)
        start = new_start
        assert_optimal(planner, grid, start, goal)


def test_move_start_into_blocked_rejected():
    blocked = np.zeros((4, 4), bool)
    blocked[0, 1] = True
    grid = Grid(np.ones((4, 4)), blocked, 8)
    planner = DStarLite(grid, (0, 0), (3, 3))
    with pytest.raises(CostValidationError):
        planner.move_start((1, 0))
    with pytest.raises(CostValidationError):
        planner.move_start((3, 3))  # goal


# ---------------------------------------------------------------------- #
# Incremental state must not reuse stale-map paths (functional guarantee)
# ---------------------------------------------------------------------- #
def test_incremental_path_valid_on_every_snapshot(rng):
    """After each change the returned path is walkable under the CURRENT grid."""
    width = height = 11
    cost = rng.random((height, width)) * 4 + 1
    blocked = rng.random((height, width)) < 0.18
    blocked[0, 0] = blocked[-1, -1] = False
    grid = Grid(cost, blocked, 8)
    start, goal = (0, 0), (width - 1, height - 1)
    planner = DStarLite(grid, start, goal)

    for step in range(40):
        result = planner.plan()
        dc, _ = dijkstra_full(grid, start, goal)
        assert result.reachable == bool(np.isfinite(dc))
        if result.reachable:
            # No edge of the path may be a leftover from a previous map.
            for a, b in zip(result.path, result.path[1:]):
                assert np.isfinite(grid.edge_cost(a, b)), (
                    f"step {step}: stale/invalid edge {a}->{b}"
                )
            assert abs(result.cost - dc) <= 1e-7 * max(1.0, dc)

        # mutate 1-3 random cells
        changed = []
        for _ in range(int(rng.integers(1, 4))):
            x = int(rng.integers(0, width))
            y = int(rng.integers(0, height))
            if (x, y) == goal or (x, y) == start:
                continue
            if rng.random() < 0.5:
                new_blocked = not grid.is_blocked(x, y)
                if (x, y) == start and new_blocked:
                    continue
                grid.blocked[y, x] = new_blocked
            else:
                grid.cost[y, x] = rng.random() * 4 + 1
            changed.append((x, y))
        grid.min_weight = float(grid.cost.min())
        if changed:
            planner.edge_costs_changed(changed)
        # sometimes relocate start to a free cell
        if rng.random() < 0.3:
            for _ in range(10):
                nx = int(rng.integers(0, width))
                ny = int(rng.integers(0, height))
                if not grid.is_blocked(nx, ny) and (nx, ny) != goal:
                    planner.move_start((nx, ny))
                    start = (nx, ny)
                    break


# ---------------------------------------------------------------------- #
# Large fuzz campaign
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", range(40))
def test_fuzz_matches_dijkstra(seed):
    rng = np.random.default_rng(1000 + seed)
    width = int(rng.integers(5, 12))
    height = int(rng.integers(5, 12))
    conn = int(rng.choice([4, 8]))
    cost = rng.random((height, width)) * 9 + 1
    blocked = rng.random((height, width)) < 0.2
    blocked[0, 0] = blocked[-1, -1] = False
    grid = Grid(cost, blocked, connectivity=conn)
    start, goal = (0, 0), (width - 1, height - 1)
    planner = DStarLite(grid, start, goal)
    assert_optimal(planner, grid, start, goal)

    for step in range(20):
        changed = []
        for _ in range(int(rng.integers(1, 5))):
            x = int(rng.integers(0, width))
            y = int(rng.integers(0, height))
            if (x, y) == goal:
                continue
            mode = rng.random()
            if mode < 0.45:
                nb = not grid.is_blocked(x, y)
                if (x, y) == start and nb:
                    continue
                grid.blocked[y, x] = nb
            else:
                grid.cost[y, x] = rng.random() * 9 + 1
            changed.append((x, y))
        grid.min_weight = float(grid.cost.min())
        if changed:
            planner.edge_costs_changed(changed)
        if rng.random() < 0.35:
            candidates = [
                (x, y)
                for x in range(width)
                for y in range(height)
                if not grid.is_blocked(x, y) and (x, y) != goal
            ]
            start = tuple(candidates[int(rng.integers(0, len(candidates)))])
            planner.move_start(start)
        assert_optimal(planner, grid, start, goal)


def test_dijkstra_helpers_agree():
    grid = make_random_grid(np.random.default_rng(3))
    start, goal = (0, 0), (grid.width - 1, grid.height - 1)
    cost = dijkstra_cost(grid, start, goal)
    path = dijkstra_path(grid, start, goal)
    if np.isfinite(cost):
        total = sum(grid.edge_cost(a, b) for a, b in zip(path, path[1:]))
        assert abs(total - cost) < 1e-9
    else:
        assert path is None
