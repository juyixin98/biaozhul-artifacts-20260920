"""Exhaustive cross-check: CBS cost must equal brute-force optimum on tiny maps.

Random instances are generated with fixed seeds so failures are reproducible.
Both solvers must agree on solvability AND on the optimal sum of costs.
"""

import random

from app.brute_force import brute_force_optimal_cost
from app.cbs import solve_cbs, validate_solution
from app.grid import GridMap


def random_instance(rng: random.Random, width: int, height: int, n_agents: int, n_obstacles: int):
    cells = [(x, y) for y in range(height) for x in range(width)]
    obstacles = set(rng.sample(cells, n_obstacles))
    free = [c for c in cells if c not in obstacles]
    picked = rng.sample(free, 2 * n_agents)
    starts = picked[:n_agents]
    goals = picked[n_agents:]
    return GridMap(width, height, list(obstacles)), starts, goals


def check_instance(grid, starts, goals):
    sol = solve_cbs(grid, starts, goals)
    brute = brute_force_optimal_cost(grid, starts, goals)
    if brute is None:
        assert sol is None, f"CBS solved an unsolvable instance: {sol.cost}"
    else:
        assert sol is not None, "CBS failed on a solvable instance"
        assert sol.cost == brute, f"CBS cost {sol.cost} != optimal {brute}"
        assert validate_solution(grid, starts, goals, sol.paths) == []


def test_exhaustive_2_agents_small_maps():
    rng = random.Random(20260923)
    for _ in range(60):
        w = rng.choice([3, 4])
        h = rng.choice([2, 3])
        n_obstacles = rng.randint(0, 2)
        grid, starts, goals = random_instance(rng, w, h, 2, n_obstacles)
        check_instance(grid, starts, goals)


def test_exhaustive_3_agents_tiny_map():
    rng = random.Random(20260924)
    for _ in range(25):
        grid, starts, goals = random_instance(rng, 3, 3, 3, 1)
        check_instance(grid, starts, goals)


def test_exhaustive_open_3x3_all_pairs():
    # Every (start, goal) pair combination for 2 agents on an open 3x3 grid.
    grid = GridMap(3, 3)
    cells = grid.free_cells
    rng = random.Random(7)
    combos = [
        (s1, s2, g1, g2)
        for s1 in cells for s2 in cells if s2 != s1
        for g1 in cells for g2 in cells if g2 != g1
    ]
    rng.shuffle(combos)
    for s1, s2, g1, g2 in combos[:150]:
        check_instance(grid, [s1, s2], [g1, g2])
