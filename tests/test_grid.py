"""Hand-checked unit tests for ray traversal, clipping and grid updates."""

import math

import numpy as np
import pytest

from occupancy_grid import CellState, OccupancyGrid, RayObservation
from occupancy_grid.grid import logit
from occupancy_grid.raycast import traverse_cells


# ---------------------------------------------------------------- geometry


def test_floor_mapping_boundaries_belong_to_higher_cell():
    from occupancy_grid import GridConfig

    grid = OccupancyGrid(GridConfig(nx=3, ny=2, resolution=1.0, origin_x=-1.0, origin_y=-2.0))
    # origin cell (ix=0, iy=0) covers x in [-1, 0), y in [-2, -1).
    assert grid.world_to_cell(-1.0, -2.0) == (0, 0)
    assert grid.world_to_cell(-0.0001, -1.0001) == (0, 0)
    # Exact boundaries map to the higher-index cell (floor convention).
    assert grid.world_to_cell(0.0, -1.0) == (1, 1)
    assert grid.world_to_cell(2.0, 0.0) == (3, 2)
    assert not grid.in_map(3, 2)
    assert grid.in_map(2, 1)


def test_traverse_horizontal_and_vertical():
    # Hand-computed in cell coordinates on a 5x3 grid.
    assert traverse_cells(0.5, 1.5, 3.5, 1.5, 5, 3) == [(0, 1), (1, 1), (2, 1), (3, 1)]
    assert traverse_cells(1.5, 0.5, 1.5, 2.5, 5, 3) == [(1, 0), (1, 1), (1, 2)]


def test_traverse_diagonal_through_corners_is_supercover():
    # Segment (0.5,0.5)->(2.5,2.5) crosses grid corners exactly at t=.25 and
    # t=.75. At an interior corner both side cells plus the diagonal cell are
    # visited (y-side first). The endpoint corner (t=1) adds the final cell.
    assert traverse_cells(0.5, 0.5, 2.5, 2.5, 5, 4) == [
        (0, 0), (0, 1), (1, 0), (1, 1),
        (1, 2), (2, 1), (2, 2),
    ]


def test_traverse_slope_two_no_corner():
    # (0.5,0.2)->(2.5,1.2): crosses x lines at t=.25,.75 and y line at t=.8.
    assert traverse_cells(0.5, 0.2, 2.5, 1.2, 5, 3) == [(0, 0), (1, 0), (2, 0), (2, 1)]


def test_traverse_negative_directions_hand_checked():
    # Walking left / down must mirror the positive-direction walks.
    assert traverse_cells(3.5, 1.5, 0.5, 1.5, 5, 3) == [(3, 1), (2, 1), (1, 1), (0, 1)]
    assert traverse_cells(1.5, 2.5, 1.5, 0.5, 5, 3) == [(1, 2), (1, 1), (1, 0)]
    # Reverse diagonal (2.5,2.5)->(0.5,0.5): corners at t=.25/.75; after the
    # second corner the ray continues into (1,0), terminating in (0,0).
    assert traverse_cells(2.5, 2.5, 0.5, 0.5, 5, 4) == [
        (2, 2), (2, 1), (1, 2), (1, 1), (1, 0), (0, 1), (0, 0),
    ]
    # Segment ending exactly on a vertical grid line: endpoint in cell ix=2.
    assert traverse_cells(0.5, 0.5, 2.0, 0.5, 5, 3) == [(0, 0), (1, 0), (2, 0)]
    # Ray grazing exactly along grid line y=1: floor mapping puts it in row 1.
    assert traverse_cells(0.5, 1.0, 2.5, 1.0, 5, 3) == [(0, 1), (1, 1), (2, 1)]


def test_traverse_fully_outside_returns_empty():
    # Segment floating entirely above the map rectangle.
    assert traverse_cells(0.5, 5.5, 3.5, 5.5, 4, 3) == []
    # Zero-length segment outside the map.
    assert traverse_cells(4.5, 1.0, 4.5, 1.0, 4, 3) == []


def test_traverse_zero_length_inside():
    assert traverse_cells(1.5, 1.5, 1.5, 1.5, 4, 3) == [(1, 1)]


def test_clipping_ray_ending_outside_map():
    # (0.5,0.5) -> (5.5,2.5) on a 4x3 grid: direction (5,2). Interior crossings
    # y=1 at t=.25 (x=1.75) and x=2 at t=.3; the ray exits at x=4, t=.7 with
    # y=1.9, i.e. through the right edge of cell (3,1) — not a corner. The
    # clipped point itself maps to ix=4 (out of map) and is excluded.
    cells = traverse_cells(0.5, 0.5, 5.5, 2.5, 4, 3)
    assert cells == [(0, 0), (1, 0), (1, 1), (2, 1), (3, 1)]
    assert all(0 <= ix < 4 and 0 <= iy < 3 for ix, iy in cells)


def test_clipping_ray_starting_outside_map():
    # Enters the map through the left edge; cells before entry are excluded.
    cells = traverse_cells(-2.5, 0.5, 2.5, 0.5, 4, 3)
    assert cells == [(0, 0), (1, 0), (2, 0)]


def _segment_aabb_overlap(x0, y0, x1, y1, ix, iy):
    """True if segment (cell coords) intersects closed AABB [ix,ix+1]x[iy,iy+1].

    Independent slab-based reference implementation used to cross-check
    traverse_cells, including grazing corner/edge contacts.
    """
    lo, hi = 0.0, 1.0
    for p, c, lo_b in ((x1 - x0, x0, ix), (y1 - y0, y0, iy)):
        if abs(p) < 1e-12:
            # Parallel to the slab: constant coordinate must lie inside it.
            if c < lo_b - 1e-12 or c > lo_b + 1 + 1e-12:
                return False
            continue
        t1, t2 = (lo_b - c) / p, (lo_b + 1 - c) / p
        lo = max(lo, min(t1, t2))
        hi = min(hi, max(t1, t2))
    return hi >= lo - 1e-12


def test_traverse_matches_bruteforce_aabb_on_random_segments():
    nx, ny = 6, 4
    case = 0
    for seed in range(4):
        rng = np.random.default_rng(20260924 + seed)
        for _ in range(500):
            x0, y0 = rng.uniform(-1.5, nx + 1.5, size=2)
            # Mix of short/long and axis-aligned-ish directions.
            if rng.random() < 0.2:
                x1, y1 = x0, y0 + rng.choice([-1, 1]) * rng.uniform(0.0, 5.0)
            elif rng.random() < 0.2:
                x1, y1 = x0 + rng.choice([-1, 1]) * rng.uniform(0.0, 7.0), y0
            else:
                x1, y1 = rng.uniform(-1.5, nx + 1.5, size=2)
            got = set(traverse_cells(x0, y0, x1, y1, nx, ny))
            expected = {
                (ix, iy)
                for ix in range(nx)
                for iy in range(ny)
                if _segment_aabb_overlap(x0, y0, x1, y1, ix, iy)
            }
            assert got == expected, (case, x0, y0, x1, y1, got ^ expected)
            case += 1


# --------------------------------------------------------------- log-odds


def make_grid(**kwargs):
    from occupancy_grid import GridConfig

    defaults = dict(nx=5, ny=4, resolution=1.0, origin_x=-2.0, origin_y=-2.0)
    defaults.update(kwargs)
    return OccupancyGrid(GridConfig(**defaults))


def test_fresh_grid_is_all_unknown_prior_half():
    grid = make_grid()
    assert np.all(grid.log_odds == 0.0)
    assert grid.state_map() == [[CellState.UNKNOWN.value] * 5 for _ in range(4)]
    assert grid.probability_at(0, 0) == pytest.approx(0.5)


def test_single_echo_ray_exact_cell_values():
    grid = make_grid()
    l_occ, l_free = logit(0.7), logit(0.3)
    result = grid.update_ray(RayObservation(ox=-1.5, oy=-0.5, ex=1.5, ey=-0.5))

    # Horizontal ray through cells (0,1)..(3,1); endpoint (3,1) occupied.
    assert result["occupied_cell"] == [3, 1]
    assert result["free_cells"] == [(0, 1), (1, 1), (2, 1)]
    for ix in range(3):
        assert grid.log_odds_at(ix, 1) == pytest.approx(l_free)
        assert grid.state_at(ix, 1) is CellState.FREE
    assert grid.log_odds_at(3, 1) == pytest.approx(l_occ)
    assert grid.state_at(3, 1) is CellState.OCCUPIED
    # Untouched cells remain exactly unknown.
    assert grid.state_at(0, 0) is CellState.UNKNOWN
    assert grid.state_at(4, 3) is CellState.UNKNOWN
    assert grid.probability_at(3, 1) == pytest.approx(0.7)
    assert grid.probability_at(0, 1) == pytest.approx(0.3)


def test_repeated_identical_rays_accumulate_and_saturate():
    grid = make_grid()
    l_occ, bound = logit(0.7), logit(0.99)
    ray = RayObservation(ox=-1.5, oy=-0.5, ex=1.5, ey=-0.5)
    values = []
    for _ in range(10):
        grid.update_ray(ray)
        values.append(grid.log_odds_at(3, 1))

    # Strictly increasing until it hits the upper clamp, then constant.
    assert values[0] == pytest.approx(l_occ)
    assert values[1] == pytest.approx(2 * l_occ)
    assert values[-1] == pytest.approx(bound)
    assert max(values) == pytest.approx(bound)
    # Free cells saturate at the symmetric lower bound.
    assert grid.log_odds_at(0, 1) == pytest.approx(-bound)
    assert grid.state_at(0, 1) is CellState.FREE
    assert grid.state_at(3, 1) is CellState.OCCUPIED


def test_contradictory_rays_cancel_exactly():
    # logit(0.3) == -logit(0.7), so one free + one occupied update returns a
    # cell to the prior (unknown).
    grid = make_grid()
    grid.update_ray(RayObservation(ox=-1.5, oy=-1.5, ex=0.5, ey=-1.5))  # (2,1) echo
    grid.update_ray(RayObservation(ox=-1.5, oy=-1.5, ex=2.5, ey=-1.5))  # (2,1) now free
    assert grid.log_odds_at(2, 1) == pytest.approx(0.0, abs=1e-12)
    assert grid.state_at(2, 1) is CellState.UNKNOWN


def test_echo_outside_map_drops_occupied_keeps_free():
    grid = make_grid()  # world x in [-2, 3), y in [-2, 2)
    result = grid.update_ray(RayObservation(ox=-1.5, oy=-0.5, ex=10.0, ey=-0.5))
    assert result["endpoint_in_map"] is False
    assert result["occupied_cell"] is None
    # Clipped free segment: nx=5 -> world x in [-2,3), cells (0,1)..(4,1).
    assert result["free_cells"] == [(0, 1), (1, 1), (2, 1), (3, 1), (4, 1)]
    for ix in range(5):
        assert grid.state_at(ix, 1) is CellState.FREE


def test_no_return_ray_marks_only_free():
    grid = make_grid()
    result = grid.update_no_return(ox=-1.5, oy=-0.5, angle=0.0, max_range=3.0)
    assert result["occupied_cell"] is None
    assert result["free_cells"] == [(0, 1), (1, 1), (2, 1), (3, 1)]
    for ix in range(4):
        assert grid.state_at(ix, 1) is CellState.FREE


def test_negative_coordinates_origin_offset():
    grid = make_grid(origin_x=-2.0, origin_y=-2.0)
    # Echo in the negative-world corner: endpoint world (-1.5,-1.5) -> cell (0,0).
    result = grid.update_ray(RayObservation(ox=-1.5, oy=-1.5, ex=-1.5, ey=-1.5))
    assert result["occupied_cell"] == [0, 0]
    assert grid.state_at(0, 0) is CellState.OCCUPIED
    # A ray wholly in negative world space crossing several cells.
    result = grid.update_ray(RayObservation(ox=-1.5, oy=-1.5, ex=-1.5, ey=0.5))
    assert result["free_cells"] == [(0, 0), (0, 1)]  # echo cell (0,2) occupied
    assert result["occupied_cell"] == [0, 2]


def test_zero_length_echo_marks_occupied_only():
    grid = make_grid()
    # (-1.5,-1.5) with origin (-2,-2) -> cell (0,0).
    result = grid.update_ray(RayObservation(ox=-1.5, oy=-1.5, ex=-1.5, ey=-1.5))
    assert result["free_cells"] == []
    assert result["occupied_cell"] == [0, 0]
    assert grid.state_at(0, 0) is CellState.OCCUPIED


def test_resolution_scaling_world_to_cell():
    grid = make_grid(resolution=0.5)
    # world x in [-2, 0.5); cell 4 covers x in [0.0, 0.5).
    assert grid.world_to_cell(0.25, -1.75) == (4, 0)
    result = grid.update_ray(RayObservation(ox=-1.75, oy=-1.75, ex=0.25, ey=-1.75))
    # 4 cells horizontally: (0,0)..(3,0) free, endpoint (4,0) occupied.
    assert result["free_cells"] == [(0, 0), (1, 0), (2, 0), (3, 0)]
    assert result["occupied_cell"] == [4, 0]


def test_clamp_never_exceeds_bounds():
    grid = make_grid()
    for _ in range(100):
        grid.update_no_return(ox=-1.5, oy=-0.5, angle=0.0, max_range=4.0)
    assert np.all(grid.log_odds >= logit(0.01) - 1e-12)
    assert np.all(grid.log_odds <= logit(0.99) + 1e-12)


def test_invalid_config_rejected():
    from occupancy_grid import GridConfig

    with pytest.raises(ValueError):
        GridConfig(nx=0, ny=1)
    with pytest.raises(ValueError):
        GridConfig(nx=1, ny=1, p_occ=0.4)
    with pytest.raises(ValueError):
        GridConfig(nx=1, ny=1, p_free=0.6)
    with pytest.raises(ValueError):
        GridConfig(nx=1, ny=1, occ_threshold=-1.0, free_threshold=1.0)


def test_cell_outside_map_query_raises():
    grid = make_grid()
    with pytest.raises(IndexError):
        grid.log_odds_at(99, 0)


def test_ray_without_endpoint_raises():
    grid = make_grid()
    with pytest.raises(ValueError):
        grid.update_ray(RayObservation(ox=0.0, oy=0.0))


# ------------------------------------------------------------ persistence


def test_export_import_roundtrip_preserves_state(tmp_path):
    import json

    grid = make_grid()
    grid.update_ray(RayObservation(ox=-1.5, oy=-0.5, ex=1.5, ey=-0.5))
    grid.update_no_return(-1.5, -1.5, math.pi / 2, 2.0)

    data = grid.to_dict()
    path = tmp_path / "grid.json"
    path.write_text(json.dumps(data))

    restored = OccupancyGrid.from_dict(json.loads(path.read_text()))
    assert restored.config == grid.config
    np.testing.assert_array_equal(restored.log_odds, grid.log_odds)
    assert restored.state_map() == grid.state_map()


def test_import_rejects_bad_shape_and_bounds(tmp_path):
    grid = make_grid()
    data = grid.to_dict()
    bad = dict(data)
    bad["log_odds"] = data["log_odds"][:-1]
    with pytest.raises(ValueError):
        OccupancyGrid.from_dict(bad)

    bad2 = dict(data)
    bad2["log_odds"] = [[99.0] * 5 for _ in range(4)]
    with pytest.raises(ValueError):
        OccupancyGrid.from_dict(bad2)

    bad3 = dict(data)
    bad3["schema_version"] = 999
    with pytest.raises(ValueError):
        OccupancyGrid.from_dict(bad3)
