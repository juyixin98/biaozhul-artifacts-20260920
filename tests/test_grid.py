"""Hand-verified tests for the occupancy grid core.

The small grids below use l_occ=1.0 and l_free=-0.5 with wide clamp
bounds so every expected value can be computed by hand.
"""

import math

import numpy as np
import pytest

from occupancy_grid.grid import CellState, OccupancyGrid
from occupancy_grid.io import load_grid, save_grid
from occupancy_grid.raycast import bresenham, clip_segment_to_box

L_OCC = 1.0
L_FREE = -0.5


def make_grid(width=5, height=5, res=1.0, ox=0.0, oy=0.0, l_min=-10.0, l_max=10.0):
    return OccupancyGrid(
        width, height, res, ox, oy,
        l_occ=L_OCC, l_free=L_FREE, l_min=l_min, l_max=l_max,
    )


# ----------------------------------------------------------------------
# Ray tracing primitives
# ----------------------------------------------------------------------
def test_bresenham_horizontal():
    assert list(bresenham(0, 0, 3, 0)) == [(0, 0), (1, 0), (2, 0), (3, 0)]


def test_bresenham_diagonal_and_reverse():
    fwd = list(bresenham(0, 0, 2, 2))
    assert fwd[0] == (0, 0) and fwd[-1] == (2, 2) and len(fwd) == 3
    # Reversed line visits the same set of cells.
    rev = list(bresenham(2, 2, 0, 0))
    assert set(rev) == set(fwd)


def test_clip_segment():
    # Segment crossing the box [0,5]x[0,5].
    seg = clip_segment_to_box(-1.0, 0.5, 10.0, 0.5, 0.0, 0.0, 5.0, 5.0)
    assert seg == (0.0, 0.5, 5.0, 0.5)
    # Fully outside.
    assert clip_segment_to_box(6.0, 0.5, 8.0, 0.5, 0.0, 0.0, 5.0, 5.0) is None


# ----------------------------------------------------------------------
# Hand-computed updates
# ----------------------------------------------------------------------
def test_single_hit_ray_hand_computed():
    g = make_grid()
    # Ray along row 0 from cell (0,0) centre to cell (3,0) centre.
    n = g.integrate_ray(0.5, 0.5, 3.5, 0.5, hit=True)
    assert n == 4
    assert g.log_odds[0, 0] == L_FREE
    assert g.log_odds[0, 1] == L_FREE
    assert g.log_odds[0, 2] == L_FREE
    assert g.log_odds[0, 3] == L_OCC
    assert g.log_odds[0, 4] == 0.0  # untouched
    assert g.state_at(0, 0) is CellState.FREE
    assert g.state_at(3, 0) is CellState.OCCUPIED
    assert g.state_at(4, 0) is CellState.UNKNOWN


def test_repeated_rays_accumulate():
    g = make_grid()
    for _ in range(3):
        g.integrate_ray(0.5, 0.5, 3.5, 0.5, hit=True)
    assert g.log_odds[0, 0] == 3 * L_FREE
    assert g.log_odds[0, 3] == 3 * L_OCC


def test_log_odds_clamping():
    g = make_grid(l_min=-1.2, l_max=1.5)
    for _ in range(5):
        g.integrate_ray(0.5, 0.5, 3.5, 0.5, hit=True)
    assert g.log_odds[0, 3] == 1.5   # clamped at l_max
    assert g.log_odds[0, 0] == -1.2  # clamped at l_min


def test_miss_marks_only_free():
    g = make_grid()
    g.integrate_ray(0.5, 0.5, 3.5, 0.5, hit=False)
    for ix in range(4):
        assert g.log_odds[0, ix] == L_FREE
    assert g.state_at(3, 0) is CellState.FREE  # endpoint NOT occupied


def test_hit_endpoint_outside_map_is_free_only():
    g = make_grid()
    # Endpoint far beyond the map edge: clipped to x=5.0 -> cell 4.
    n = g.integrate_ray(0.5, 0.5, 10.5, 0.5, hit=True)
    assert n == 5
    for ix in range(5):
        assert g.log_odds[0, ix] == L_FREE
    assert not (g.log_odds > 0).any()


def test_ray_fully_outside_map_is_ignored():
    g = make_grid()
    n = g.integrate_ray(10.5, 0.5, 12.5, 0.5, hit=True)
    assert n == 0
    assert (g.log_odds == 0.0).all()


def test_negative_world_coordinates():
    # Map covers [-2.5, 2.5) x [-2.5, 2.5).
    g = make_grid(ox=-2.5, oy=-2.5)
    assert g.world_to_grid(-2.4, -2.4) == (0, 0)
    assert g.world_to_grid(-0.1, -0.1) == (2, 2)
    assert g.world_to_grid(-3.0, 0.0) is None
    # Ray from (-1.5, -1.5) to (1.5, -1.5): cells (1,1)..(4,1).
    g.integrate_ray(-1.5, -1.5, 1.5, -1.5, hit=True)
    assert g.log_odds[1, 1] == L_FREE
    assert g.log_odds[1, 2] == L_FREE
    assert g.log_odds[1, 3] == L_FREE
    assert g.log_odds[1, 4] == L_OCC


def test_scan_hit_and_miss():
    g = make_grid()
    # One hit beam (range 2.0) and one miss (range 10.0 > max_range 3.0).
    g.integrate_scan(
        pose_x=0.5, pose_y=0.5, pose_theta=0.0,
        angles=[0.0], ranges=[2.0], max_range=5.0,
    )
    assert g.log_odds[0, 0] == L_FREE
    assert g.log_odds[0, 1] == L_FREE
    assert g.log_odds[0, 2] == L_OCC

    g2 = make_grid()
    g2.integrate_scan(
        pose_x=0.5, pose_y=0.5, pose_theta=0.0,
        angles=[0.0], ranges=[10.0], max_range=3.0,
    )
    # Miss truncated at max_range=3.0 -> endpoint (3.5, 0.5), all free.
    for ix in range(4):
        assert g2.log_odds[0, ix] == L_FREE
    assert not (g2.log_odds > 0).any()


def test_scan_with_rotated_pose():
    g = make_grid()
    # Beam pointing straight up from (0.5, 0.5).
    g.integrate_scan(
        pose_x=0.5, pose_y=0.5, pose_theta=math.pi / 2,
        angles=[0.0], ranges=[2.0], max_range=5.0,
    )
    assert g.log_odds[0, 0] == L_FREE
    assert g.log_odds[1, 0] == L_FREE
    assert g.log_odds[2, 0] == L_OCC


def test_probability_grid():
    g = make_grid()
    g.integrate_ray(0.5, 0.5, 3.5, 0.5, hit=True)
    p = g.probability_grid()
    assert p[0, 4] == pytest.approx(0.5)  # unknown
    assert p[0, 3] == pytest.approx(1.0 / (1.0 + math.exp(-L_OCC)))
    assert p[0, 0] == pytest.approx(1.0 / (1.0 + math.exp(-L_FREE)))


# ----------------------------------------------------------------------
# Save / load round-trip
# ----------------------------------------------------------------------
def test_save_load_roundtrip(tmp_path):
    g = make_grid(ox=-2.5, oy=-2.5)
    g.integrate_ray(-1.5, -1.5, 1.5, -1.5, hit=True)
    g.integrate_ray(-1.5, -0.5, 0.5, -0.5, hit=False)
    path = save_grid(g, tmp_path / "map")
    assert path.suffix == ".npz"

    g2 = load_grid(path)
    assert g2.width == g.width and g2.height == g.height
    assert g2.resolution == g.resolution
    assert g2.origin_x == g.origin_x and g2.origin_y == g.origin_y
    assert g2.l_occ == g.l_occ and g2.l_free == g.l_free
    assert g2.l_min == g.l_min and g2.l_max == g.l_max
    np.testing.assert_array_equal(g2.log_odds, g.log_odds)
    np.testing.assert_array_equal(g2.state_grid(), g.state_grid())


def test_load_rejects_bad_version(tmp_path):
    g = make_grid()
    path = save_grid(g, tmp_path / "m.npz")
    data = dict(np.load(path))
    data["format_version"] = np.int64(999)
    np.savez(tmp_path / "bad.npz", **data)
    with pytest.raises(ValueError, match="format version"):
        load_grid(tmp_path / "bad.npz")
