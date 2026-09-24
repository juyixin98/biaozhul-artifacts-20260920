"""Unit tests for the grid map, clearance field and collision checker."""

import math

import numpy as np
import pytest

from hybrid_astar.collision import CollisionChecker
from hybrid_astar.grid_map import GridMap
from hybrid_astar.vehicle import Vehicle


def make_map():
    # 10x10 cells, res 0.5 -> 5 m x 5 m; single obstacle cell at (row 5, col 5)
    data = np.zeros((10, 10), dtype=np.uint8)
    data[5, 5] = 1
    return GridMap(data, resolution=0.5)


def test_clearance_matches_distance_transform():
    gm = make_map()
    # cell (row 5, col 2) is 3 cells left of the obstacle -> 1.5 m
    assert gm.clearance[5, 2] == pytest.approx(1.5)
    # diagonal neighbor of the obstacle
    assert gm.clearance[4, 4] == pytest.approx(0.5 * math.sqrt(2))
    # the obstacle cell itself has zero clearance
    assert gm.clearance[5, 5] == pytest.approx(0.0)


def test_out_of_bounds_is_blocked():
    gm = make_map()
    assert gm.is_occupied(-1.0, 0.0)
    assert gm.is_occupied(0.0, 99.0)
    assert gm.clearance_at(-1.0, 0.0) == -math.inf


def test_collision_checker_respects_footprint_radius():
    gm = make_map()
    vehicle = Vehicle(length=2.0, width=1.0, margin=0.1)  # radius 0.6
    checker = CollisionChecker(gm, vehicle)
    # pose centered on the obstacle cell must collide
    assert not checker.is_pose_free(2.5, 2.5, 0.0)
    # far corner is free
    assert checker.is_pose_free(0.25, 0.25, 0.0)
    # facing +x, the front circle reaches 0.4 m ahead of the pose:
    # pose at x=1.5 puts the front circle at x=1.9 (clearance 0.5 < 0.6)
    assert not checker.is_pose_free(1.5, 2.5, 0.0)
    # pose at x=1.0 puts the front circle at x=1.4 (clearance >= 0.6... check:
    # cell col round(1.4/0.5)=3, clearance 1.0 m) -> free
    assert checker.is_pose_free(1.0, 2.5, 0.0)


def test_vehicle_circle_offsets_cover_body():
    v = Vehicle(length=2.0, width=1.0, margin=0.1, n_circles=3)
    offs = v.circle_offsets
    r = v.circle_radius
    # circles span the body: first/last center within one radius of the ends
    assert abs(offs[0] - (-(v.length / 2 - r))) < 1e-9
    assert abs(offs[-1] - (v.length / 2 - r)) < 1e-9
    # consecutive circles overlap (no gap in coverage)
    gaps = np.diff(offs)
    assert np.all(gaps <= 2 * r)


def test_circle_centers_rotate_with_pose():
    v = Vehicle(length=2.0, width=1.0, margin=0.0, n_circles=3)
    centers = v.circle_centers(0.0, 0.0, math.pi / 2)  # facing +y
    # offsets along the heading direction -> centers spread in y, not x
    assert np.allclose(centers[:, 0], 0.0, atol=1e-12)
    assert centers[-1, 1] > centers[0, 1]
