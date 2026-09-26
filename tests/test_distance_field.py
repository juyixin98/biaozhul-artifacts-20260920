"""Validation of the fast exact EDT against the brute-force reference."""

import math

import numpy as np
import pytest

from edf import brute_force_field, distance_field

CELL_SIZES = [(1.0, 1.0), (0.5, 0.25), (0.1, 0.3), (2.0, 2.0)]


def random_grid(rng, h, w, p):
    return (rng.random((h, w)) < p).tolist()


def assert_fields_equal(dist_fast, src_fast, dist_ref, src_ref):
    # Bit-exact: both sides evaluate the same float expression in the same
    # order, so == (not approx) is the correct check.
    assert np.array_equal(dist_fast, dist_ref, equal_nan=True)
    assert src_fast == src_ref


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("cell_size", CELL_SIZES)
def test_matches_brute_force_random(seed, cell_size):
    rng = np.random.default_rng(seed)
    h = int(rng.integers(1, 9))
    w = int(rng.integers(1, 9))  # non-square grids included
    p = float(rng.uniform(0.05, 0.6))
    grid = random_grid(rng, h, w, p)
    dist_fast, src_fast = distance_field(grid, cell_size)
    dist_ref, src_ref = brute_force_field(grid, cell_size)
    assert_fields_equal(dist_fast, src_fast, dist_ref, src_ref)


@pytest.mark.parametrize("cell_size", CELL_SIZES)
def test_matches_brute_force_larger_map(cell_size):
    rng = np.random.default_rng(123)
    grid = random_grid(rng, 17, 23, 0.15)
    dist_fast, src_fast = distance_field(grid, cell_size)
    dist_ref, src_ref = brute_force_field(grid, cell_size)
    assert_fields_equal(dist_fast, src_fast, dist_ref, src_ref)


def test_all_empty_map():
    grid = [[False] * 5 for _ in range(3)]
    dist, sources = distance_field(grid, (0.5, 0.5))
    assert np.isinf(dist).all()
    assert all(src is None for row in sources for src in row)


def test_all_obstacle_map():
    grid = [[True] * 4 for _ in range(4)]
    dist, sources = distance_field(grid, (0.5, 0.25))
    assert (dist == 0.0).all()
    for y in range(4):
        for x in range(4):
            assert sources[y][x] == (x, y)


def test_single_obstacle_pythagorean():
    grid = [[False] * 5 for _ in range(5)]
    grid[0][0] = True
    dist, sources = distance_field(grid, (1.0, 1.0))
    assert dist[0][0] == 0.0
    assert dist[4][3] == pytest.approx(5.0)  # 3-4-5 triangle
    assert sources[4][3] == (0, 0)
    assert dist[0][4] == pytest.approx(4.0)


def test_tie_two_obstacles_horizontal():
    grid = [[True, False, True]]
    dist, sources = distance_field(grid, (1.0, 1.0))
    assert dist[0][1] == 1.0
    # row-major tie-break: smallest (row, col) wins
    assert sources[0][1] == (0, 0)


def test_tie_two_obstacles_vertical():
    grid = [[True], [False], [True]]
    dist, sources = distance_field(grid, (1.0, 1.0))
    assert dist[1][0] == 1.0
    assert sources[1][0] == (0, 0)


def test_tie_four_corners():
    h = w = 5
    grid = [[False] * w for _ in range(h)]
    for x, y in [(0, 0), (4, 0), (0, 4), (4, 4)]:
        grid[y][x] = True
    dist, sources = distance_field(grid, (1.0, 1.0))
    assert dist[2][2] == pytest.approx(math.sqrt(8.0))
    assert sources[2][2] == (0, 0)


def test_tie_three_obstacles_circumcenter():
    # (2,2) is the circumcentre of (0,0), (4,0), (0,4): all at sqrt(8).
    h = w = 5
    grid = [[False] * w for _ in range(h)]
    for x, y in [(0, 0), (4, 0), (0, 4)]:
        grid[y][x] = True
    dist, sources = distance_field(grid, (1.0, 1.0))
    assert dist[2][2] == pytest.approx(math.sqrt(8.0))
    assert sources[2][2] == (0, 0)


def test_tie_diagonal_pythagorean():
    # (3,4) is at distance 5 from both (0,0) and (6,8).
    grid = [[False] * 7 for _ in range(9)]
    grid[0][0] = True
    grid[8][6] = True
    dist, sources = distance_field(grid, (1.0, 1.0))
    assert dist[4][3] == pytest.approx(5.0)
    assert sources[4][3] == (0, 0)


def test_tie_non_square_cells():
    # Vertical neighbours tie at the midpoint only when measured in metres.
    grid = [[True], [False], [True]]
    dist, sources = distance_field(grid, (1.0, 0.5))
    assert dist[1][0] == pytest.approx(0.5)
    assert sources[1][0] == (0, 0)


def test_non_square_cells_distances():
    grid = [[False] * 4 for _ in range(4)]
    grid[0][0] = True
    dist, sources = distance_field(grid, (0.5, 2.0))
    # 3 cells over in x, 1 cell down in y: sqrt(1.5^2 + 2^2) = 2.5
    assert dist[1][3] == pytest.approx(2.5)
    assert sources[1][3] == (0, 0)


def test_obstacles_on_map_boundary():
    rng = np.random.default_rng(99)
    h, w = 7, 11
    grid = [[False] * w for _ in range(h)]
    # obstacles exactly on all four borders and all four corners
    for x in range(0, w, 2):
        grid[0][x] = True
        grid[h - 1][x] = True
    for y in range(0, h, 3):
        grid[y][0] = True
        grid[y][w - 1] = True
    dist_fast, src_fast = distance_field(grid, (0.25, 0.75))
    dist_ref, src_ref = brute_force_field(grid, (0.25, 0.75))
    assert_fields_equal(dist_fast, src_fast, dist_ref, src_ref)


def test_single_cell_grid():
    dist, sources = distance_field([[True]], (0.3, 0.7))
    assert dist[0, 0] == 0.0
    assert sources[0][0] == (0, 0)
    dist, sources = distance_field([[False]], (0.3, 0.7))
    assert math.isinf(dist[0, 0])
    assert sources[0][0] is None


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        distance_field([True, False])  # not 2D
    with pytest.raises(ValueError):
        distance_field([[True]], (0.0, 1.0))  # non-positive cell
    with pytest.raises(ValueError):
        distance_field([[True]], (1.0, math.inf))  # non-finite cell
    with pytest.raises(ValueError):
        brute_force_field([[True]], (-1.0, 1.0))
