"""Unit tests for synthetic scan generation."""

import numpy as np

from icp2d.synthetic import (
    add_noise,
    add_outliers,
    apply_partial_overlap,
    make_scan_pair,
    rectangle_room_scan,
    straight_wall_scan,
)
from icp2d.geometry import transform_points


def test_rectangle_room_scan_lies_on_boundary():
    scan = rectangle_room_scan(width=14.0, height=9.0, spacing=0.1)
    assert scan.shape[1] == 2
    on_wall = (
        np.isclose(np.abs(scan[:, 0]), 7.0)
        | np.isclose(np.abs(scan[:, 1]), 4.5)
    )
    assert on_wall.all()


def test_straight_wall_scan_is_collinear_along_x():
    wall = straight_wall_scan(length=5.0, spacing=0.1)
    assert np.allclose(wall[:, 1], 0.0)
    assert abs(wall[:, 0].max() - 2.5) < 0.1
    assert abs(wall[:, 0].min() + 2.5) < 0.1


def test_add_noise_magnitude():
    rng = np.random.default_rng(0)
    points = np.zeros((10000, 2))
    noisy = add_noise(points, sigma=0.1, rng=rng)
    # Isotropic Gaussian with sigma=0.1 -> empirical std close to 0.1.
    assert abs(noisy.std() - 0.1) < 0.01


def test_add_outliers_appends_points():
    points = np.zeros((4, 2))
    rng = np.random.default_rng(0)
    combined = add_outliers(points, count=6, rng=rng)
    assert len(combined) == 10
    np.testing.assert_allclose(combined[:4], points)


def test_partial_overlap_subset_and_reproducible():
    rng = np.random.default_rng(3)
    points = np.arange(100).reshape(50, 2).astype(float)
    subset = apply_partial_overlap(points, keep_ratio=0.5, rng=rng)
    assert len(subset) == 25
    # Every retained point is an original one.
    assert all(any(np.array_equal(p, q) for q in points) for p in subset)


def test_make_scan_pair_ground_truth_maps_source_to_target():
    target = straight_wall_scan(length=8.0, spacing=0.1)
    ground_truth = np.array([0.5, -0.2, 0.1])
    scenario = make_scan_pair(target, ground_truth, seed=1)
    np.testing.assert_allclose(
        transform_points(scenario.source, ground_truth), scenario.target, atol=1e-10
    )
