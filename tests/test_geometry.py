"""Geometry primitives: planes, tilts, normals, collinearity handling."""

import math

import numpy as np
import pytest

from app.segmentation.geometry import (
    fit_plane,
    local_normals,
    orient_normal,
    plane_from_three,
)


def test_plane_through_three_horizontal():
    pts = np.array([[0.0, 0.0, 1.0],
                    [1.0, 0.0, 1.0],
                    [0.0, 1.0, 1.0]])
    p = plane_from_three(pts)
    assert np.allclose(p.normal, [0.0, 0.0, 1.0])
    assert p.offset == pytest.approx(-1.0)
    assert p.tilt_deg == pytest.approx(0.0, abs=1e-9)
    assert np.allclose(p.signed_distance(pts), 0.0, atol=1e-12)


def test_plane_normal_orientation_flips_to_up():
    # Order the points so the raw cross product points down.
    pts = np.array([[0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]])
    a, b, c = pts
    raw = np.cross(b - a, c - a)
    assert raw[2] < 0
    p = plane_from_three(pts, orientation="up")
    assert p.normal[2] > 0


def test_tilt_of_inclined_plane():
    ang = 15.0
    # Plane z = tan(ang) * x
    pts = np.array([[0.0, 0.0, 0.0],
                    [1.0, 0.0, math.tan(math.radians(ang))],
                    [0.0, 1.0, 0.0]])
    p = plane_from_three(pts)
    assert p.tilt_deg == pytest.approx(ang, abs=1e-8)


def test_vertical_wall_tilt_is_ninety():
    pts = np.array([[1.0, 0.0, 0.0],
                    [1.0, 1.0, 0.0],
                    [1.0, 0.0, 1.0]])
    p = plane_from_three(pts)
    assert p.tilt_deg == pytest.approx(90.0, abs=1e-8)


def test_collinear_three_points_rejected():
    pts = np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 1.0], [2.0, 2.0, 2.0]])
    with pytest.raises(ValueError):
        plane_from_three(pts)


def test_fit_plane_rejects_collinear_and_tiny_sets():
    with pytest.raises(ValueError):
        fit_plane(np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]))
    with pytest.raises(ValueError):
        fit_plane(np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]))


def test_fit_plane_recovers_noisy_horizontal():
    rng = np.random.default_rng(0)
    xs, ys = np.meshgrid(np.arange(0.0, 2.0, 0.2), np.arange(0.0, 2.0, 0.2))
    pts = np.column_stack([xs.ravel(), ys.ravel(),
                           0.5 + rng.normal(scale=0.002, size=xs.size)])
    p = fit_plane(pts)
    assert p.tilt_deg < 1.0
    assert abs(p.offset + 0.5) < 0.01
    assert p.rms < 0.005


def test_orient_normal_helper():
    n = orient_normal(np.array([0.0, 0.0, -3.0]), "up")
    assert n[2] == pytest.approx(1.0)
    n = orient_normal(np.array([0.0, 0.0, 3.0]), "down")
    assert n[2] == pytest.approx(-1.0)
    with pytest.raises(ValueError):
        orient_normal(np.zeros(3))


def test_local_normals_horizontal_and_wall():
    rng = np.random.default_rng(1)
    xs, ys = np.meshgrid(np.arange(0.0, 2.0, 0.25), np.arange(0.0, 2.0, 0.25))
    ground = np.column_stack([xs.ravel(), ys.ravel(),
                              rng.normal(scale=0.002, size=xs.size)])
    ys2, zs = np.meshgrid(np.arange(0.0, 2.0, 0.25), np.arange(0.5, 2.0, 0.25))
    wall = np.column_stack([np.full(ys2.size, 3.0), ys2.ravel(), zs.ravel()])
    pts = np.vstack([ground, wall])
    normals, valid = local_normals(pts, k=8)
    assert valid.all()
    # Ground normals point up; wall normals are horizontal.
    assert np.all(normals[:len(ground), 2] > 0.95)
    assert np.all(np.abs(normals[len(ground):, 2]) < 0.3)


def test_local_normals_duplicates_invalid():
    # All points identical: no unique neighbourhood -> invalid normals.
    pts = np.tile(np.array([1.0, 2.0, 3.0]), (10, 1))
    normals, valid = local_normals(pts, k=5)
    assert not valid.any()
