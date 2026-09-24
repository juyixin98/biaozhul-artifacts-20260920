"""CBS correctness tests: crossroad, single-channel corridor, unsolvable,
goal occupation, and edge-conflict handling."""

from app.brute_force import brute_force_optimal_cost
from app.cbs import solve_cbs, validate_solution
from app.grid import GridMap
from examples.instances import CROSSROAD, CORRIDOR_UNSOLVABLE, CORRIDOR_WITH_BAYS


def make_grid(spec):
    return GridMap(
        spec["grid"]["width"],
        spec["grid"]["height"],
        [tuple(o) for o in spec["grid"]["obstacles"]],
    )


def starts_goals(spec):
    starts = [tuple(a["start"]) for a in spec["agents"]]
    goals = [tuple(a["goal"]) for a in spec["agents"]]
    return starts, goals


def solve_spec(spec):
    grid = make_grid(spec)
    starts, goals = starts_goals(spec)
    return grid, starts, goals, solve_cbs(grid, starts, goals)


def test_crossroad_solved_optimally():
    grid, starts, goals, sol = solve_spec(CROSSROAD)
    assert sol is not None
    # Each agent needs 4 steps; one of them must wait exactly 1 step.
    assert sol.cost == 9
    assert sol.cost == brute_force_optimal_cost(grid, starts, goals)
    assert validate_solution(grid, starts, goals, sol.paths) == []
    # The conflict forces a non-root constraint tree.
    assert sol.stats.high_level_nodes > 1
    assert sol.stats.conflicts_detected >= 1


def test_corridor_with_bays_solvable():
    grid, starts, goals, sol = solve_spec(CORRIDOR_WITH_BAYS)
    assert sol is not None
    assert validate_solution(grid, starts, goals, sol.paths) == []
    assert sol.cost == brute_force_optimal_cost(grid, starts, goals)


def test_strict_corridor_swap_unsolvable():
    grid, starts, goals, sol = solve_spec(CORRIDOR_UNSOLVABLE)
    assert sol is None
    assert brute_force_optimal_cost(grid, starts, goals) is None


def test_goal_occupation_blocks_latecomer():
    # 1-wide 5x1 corridor: agent 0 parks at (2, 0) on agent 1's only route,
    # and goal occupation means it never moves again -> unsolvable.
    grid = GridMap(5, 1)
    starts = [(0, 0), (4, 0)]
    goals = [(2, 0), (0, 0)]
    sol = solve_cbs(grid, starts, goals)
    assert sol is None
    assert brute_force_optimal_cost(grid, starts, goals) is None


def test_goal_occupation_forces_wait():
    # 3x2 open grid: agent 0 parks at (1, 0); agent 1 passes through (1, 0).
    grid = GridMap(3, 2)
    starts = [(0, 0), (2, 0)]
    goals = [(1, 0), (0, 0)]
    sol = solve_cbs(grid, starts, goals)
    assert sol is not None
    assert validate_solution(grid, starts, goals, sol.paths) == []
    assert sol.cost == brute_force_optimal_cost(grid, starts, goals)
    # Agent 0's path must end at (1,0) and stay there.
    assert sol.paths[0][-1] == (1, 0)


def test_edge_conflict_swap_is_forbidden():
    # Two adjacent agents swapping cells: without the edge-conflict rule the
    # cost would be 2; the swap is illegal so one agent must detour/wait.
    grid = GridMap(3, 2)
    starts = [(0, 0), (1, 0)]
    goals = [(1, 0), (0, 0)]
    sol = solve_cbs(grid, starts, goals)
    assert sol is not None
    assert sol.cost > 2
    assert validate_solution(grid, starts, goals, sol.paths) == []
    assert sol.cost == brute_force_optimal_cost(grid, starts, goals)


def test_start_equals_goal():
    grid = GridMap(3, 3)
    starts = [(0, 0), (2, 2)]
    goals = [(0, 0), (0, 2)]
    sol = solve_cbs(grid, starts, goals)
    assert sol is not None
    assert sol.paths[0] == [(0, 0)]
    assert validate_solution(grid, starts, goals, sol.paths) == []
    assert sol.cost == brute_force_optimal_cost(grid, starts, goals)


def test_unreachable_goal_unsolvable():
    grid = GridMap(3, 3, obstacles=[(1, 0), (1, 1), (1, 2)])
    sol = solve_cbs(grid, [(0, 0)], [(2, 2)])
    assert sol is None
    assert brute_force_optimal_cost(grid, [(0, 0)], [(2, 2)]) is None
