"""Seeded RANSAC: reliability gates, determinism, duplicates, tilt refusal."""

import math

import numpy as np
import pytest

from app.segmentation.ransac import (
    REASON_LOW_INLIER_RATIO,
    REASON_PLANE_TOO_ROUGH,
    REASON_TILT_EXCEEDED,
    REASON_TOO_FEW_INLIERS,
    REASON_TOO_FEW_POINTS,
    RansacConfig,
    ransac_plane,
)


def _flat_ground(rng, n_per=12, sigma=0.01):
    xs = rng.uniform(0, 4, n_per * n_per)
    ys = rng.uniform(0, 4, n_per * n_per)
    zs = rng.normal(scale=sigma, size=xs.size)
    return np.column_stack([xs, ys, zs])


def test_reliable_flat_ground():
    rng = np.random.default_rng(7)
    pts = _flat_ground(rng)
    res = ransac_plane(pts, RansacConfig(rng_seed=42))
    assert res.reliable, res.reason
    assert res.plane is not None
    assert res.plane.tilt_deg < 2.0
    assert res.plane.inlier_ratio > 0.95
    assert res.plane.rms < 0.02


def test_deterministic_with_seed():
    rng = np.random.default_rng(7)
    pts = _flat_ground(rng)
    a = ransac_plane(pts, RansacConfig(rng_seed=12345))
    b = ransac_plane(pts, RansacConfig(rng_seed=12345))
    assert a.reliable and b.reliable
    assert np.array_equal(a.inlier_mask, b.inlier_mask)
    assert np.allclose(a.plane.normal, b.plane.normal)
    assert a.plane.offset == b.plane.offset
    c = ransac_plane(pts, RansacConfig(rng_seed=999))
    # Different seed may still converge, but mask can legally differ;
    # at minimum the fitted normals must agree on clean data.
    assert np.allclose(a.plane.normal, c.plane.normal, atol=1e-3)


def test_slope_inside_limit_accepted_outside_rejected():
    rng = np.random.default_rng(8)
    xs = rng.uniform(0, 6, 400)
    ys = rng.uniform(0, 4, 400)
    for ang, should_work in [(12.0, True), (30.0, False)]:
        zs = math.tan(math.radians(ang)) * xs + rng.normal(scale=0.01, size=400)
        pts = np.column_stack([xs, ys, zs])
        res = ransac_plane(pts, RansacConfig(rng_seed=42, max_tilt_deg=20.0))
        if should_work:
            assert res.reliable, res.reason
            assert res.plane.tilt_deg == pytest.approx(ang, abs=1.5)
        else:
            assert not res.reliable
            assert res.reason == REASON_TILT_EXCEEDED
            # Refusal must not return a plane or mask.
            assert res.plane is None
            assert not res.inlier_mask.any()


def test_too_few_points_gate():
    pts = np.random.default_rng(3).normal(size=(8, 3))
    res = ransac_plane(pts, RansacConfig(min_points=12))
    assert not res.reliable
    assert res.reason == REASON_TOO_FEW_POINTS


def test_low_inlier_ratio_gate():
    # A small clean ground band drowned in uniformly scattered 3-D noise.
    # With a tight distance threshold no noise slab can gather many inliers,
    # so the best candidate is the ground band itself at ~14% inlier ratio.
    rng = np.random.default_rng(9)
    ground = _flat_ground(rng, n_per=4)            # 16 ground-ish
    ground[:, 2] *= 0.5
    noise = rng.uniform([0.0, 0.0, 0.5], [4.0, 4.0, 4.0], size=(100, 3))
    pts = np.vstack([ground, noise])
    res = ransac_plane(
        pts,
        RansacConfig(rng_seed=42, distance_threshold=0.03, max_rms=0.02,
                     min_inlier_ratio=0.5, min_points=10, min_inlier_count=6),
    )
    assert not res.reliable
    assert res.reason in (REASON_LOW_INLIER_RATIO, REASON_TOO_FEW_INLIERS)


def test_largest_plane_is_not_auto_ground():
    """A wall is the only large plane present; it must still be refused."""
    rng = np.random.default_rng(10)
    ys, zs = np.meshgrid(np.arange(0, 4, 0.2), np.arange(0.5, 2.5, 0.2))
    wall = np.column_stack([np.full(ys.size, 2.0) + rng.normal(scale=0.005, size=ys.size),
                            ys.ravel(), zs.ravel()])
    res = ransac_plane(wall, RansacConfig(rng_seed=42))
    assert not res.reliable
    assert res.reason == REASON_TILT_EXCEEDED


def test_duplicates_do_not_break_sampling():
    rng = np.random.default_rng(11)
    base = _flat_ground(rng, n_per=5)
    # Repeat rows many times (coordinate scanner artefacts).
    dup_idx = rng.integers(0, len(base), size=120)
    pts = np.vstack([base, base[dup_idx]])
    res = ransac_plane(pts, RansacConfig(rng_seed=42))
    assert res.reliable, res.reason
    assert res.stats["unique_count"] == len(base)
    # Every duplicate of an inlier coordinate inherits the inlier label.
    assert res.inlier_mask[len(base):].mean() > 0.9


def test_all_coincident_points_undecidable():
    pts = np.tile(np.array([1.0, 2.0, 3.0]), (20, 1))
    res = ransac_plane(pts)
    assert not res.reliable


def test_roughness_gate():
    # Inliers spread thickly around z=0 (0.08 m RMS): plane exists but is rough.
    rng = np.random.default_rng(12)
    xs = rng.uniform(0, 3, 200)
    ys = rng.uniform(0, 3, 200)
    zs = rng.normal(scale=0.08, size=200)
    pts = np.column_stack([xs, ys, zs])
    res = ransac_plane(pts, RansacConfig(rng_seed=42, distance_threshold=0.3,
                                         max_rms=0.02, max_tilt_deg=20.0))
    assert not res.reliable
    assert res.reason == REASON_PLANE_TOO_ROUGH


def test_seed_sampling_finds_low_plane_with_few_iterations():
    # Ground band at z~0 plus a smaller (sub-50%) inclined clutter ribbon
    # above: the majority plane is the ground; seeded sampling must lock onto
    # it even with a small iteration budget.
    rng = np.random.default_rng(13)
    ground = _flat_ground(rng, n_per=7)          # 49 ground points
    xs = rng.uniform(0, 4, 40)
    clutter = np.column_stack([xs, rng.uniform(0, 4, 40),
                               1.0 + 0.4 * xs + rng.normal(scale=0.01, size=40)])
    pts = np.vstack([ground, clutter])
    res = ransac_plane(pts, RansacConfig(rng_seed=42, min_iterations=10,
                                         max_iterations=60,
                                         min_inlier_ratio=0.5,
                                         min_inlier_count=10,
                                         max_tilt_deg=25.0))
    assert res.reliable, res.reason
    assert abs(res.plane.normal[2]) == pytest.approx(1.0, abs=0.05)
    assert res.plane.tilt_deg < 5.0
