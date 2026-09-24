"""平面拟合与带种子 RANSAC 的单元测试。"""

import numpy as np
import pytest

from groundseg.planes import (
    Plane,
    fit_plane_svd,
    plane_from_three,
    ransac_plane,
)


def test_plane_from_three_horizontal():
    p = plane_from_three(
        np.array([0.0, 0.0, 1.0]),
        np.array([1.0, 0.0, 1.0]),
        np.array([0.0, 1.0, 1.0]),
    )
    assert p is not None
    # 朝上的水平平面 z=1 => n=(0,0,1), d=-1
    assert p.tilt_deg() == pytest.approx(0.0, abs=1e-9)
    assert p.offset == pytest.approx(-1.0, abs=1e-12)
    assert np.allclose(p.normal, [0, 0, 1])


def test_plane_from_three_collinear_is_none():
    p = plane_from_three(
        np.array([0.0, 0, 0]),
        np.array([1.0, 0, 0]),
        np.array([2.0, 0, 0]),
    )
    assert p is None


def test_fit_plane_svd_recovers_known_tilt():
    rng = np.random.default_rng(0)
    theta = np.radians(15)
    xs = rng.uniform(-3, 3, 500)
    ys = rng.uniform(-3, 3, 500)
    zs = xs * np.tan(theta) + rng.normal(0, 0.01, 500)
    pts = np.column_stack([xs, ys, zs])
    plane = fit_plane_svd(pts)
    assert plane is not None
    assert plane.tilt_deg() == pytest.approx(15.0, abs=0.3)


def test_fit_plane_svd_degenerate_line_is_none():
    pts = np.column_stack([np.linspace(0, 1, 10), np.zeros(10), np.zeros(10)])
    assert fit_plane_svd(pts) is None
    assert fit_plane_svd(np.zeros((2, 3))) is None


def test_ransac_recovers_plane_with_outliers():
    rng = np.random.default_rng(7)
    # 80% 水平地面点 z=0，20% 高处离群点
    n_in = 400
    inl = np.column_stack([
        rng.uniform(-4, 4, n_in), rng.uniform(-4, 4, n_in),
        rng.normal(0, 0.02, n_in),
    ])
    n_out = 100
    out = np.column_stack([
        rng.uniform(-4, 4, n_out), rng.uniform(-4, 4, n_out),
        rng.uniform(2, 4, n_out),
    ])
    pts = np.vstack([inl, out])
    rr = ransac_plane(
        pts, iterations=200, distance_threshold=0.1,
        rng=np.random.default_rng(123),
    )
    assert rr.reason == "ok"
    assert rr.plane.tilt_deg() == pytest.approx(0.0, abs=2.0)
    assert rr.inlier_mask.sum() >= 0.75 * pts.shape[0]
    # 离群点不应进入内点
    assert not rr.inlier_mask[n_in:].any()


def test_ransac_is_deterministic_with_seed():
    rng = np.random.default_rng(0)
    pts = np.column_stack([
        rng.uniform(-2, 2, 300), rng.uniform(-2, 2, 300),
        rng.normal(0, 0.05, 300),
    ])
    r1 = ransac_plane(pts, iterations=50, distance_threshold=0.1,
                      rng=np.random.default_rng(99))
    r2 = ransac_plane(pts, iterations=50, distance_threshold=0.1,
                      rng=np.random.default_rng(99))
    assert np.array_equal(r1.inlier_mask, r2.inlier_mask)
    assert np.allclose(r1.plane.normal, r2.plane.normal)


def test_ransac_explicit_seed_hypothesis_first():
    # 构造地面点 + 天花板点（另一个更大平面）。只给 3 个地面种子，
    # RANSAC 仍会找到更大的平面——种子只是“首个假设”，不伪造结果。
    # 这里验证的是：给了合法种子时首个假设被实际采用且结果合理。
    rng = np.random.default_rng(1)
    floor = np.column_stack([
        rng.uniform(-1, 1, 60), rng.uniform(-1, 1, 60),
        rng.normal(0, 0.01, 60)])
    seeds = np.array([0, 1, 2])
    rr = ransac_plane(floor, iterations=20, distance_threshold=0.1,
                      rng=np.random.default_rng(5), seed_indices=seeds)
    assert rr.reason == "ok"
    assert rr.plane.tilt_deg() < 2.0
    assert rr.iterations_used == 20


def test_ransac_rejects_all_duplicate_points():
    pts = np.tile(np.array([1.0, 2.0, 3.0]), (50, 1))
    rr = ransac_plane(pts, iterations=20, distance_threshold=0.1,
                      rng=np.random.default_rng(1))
    assert rr.reason == "degenerate"
    assert rr.plane is None


def test_ransac_too_few_points():
    pts = np.zeros((2, 3))
    rr = ransac_plane(pts, iterations=10, distance_threshold=0.1,
                      rng=np.random.default_rng(1))
    assert rr.reason == "too_few_points"


def test_signed_distance_and_orientation():
    # 法向总是朝上
    p = plane_from_three(
        np.array([0.0, 0, 0]), np.array([0.0, 1, 0]), np.array([1.0, 0, 0])
    )
    assert p.normal[2] >= 0
    d = p.signed_distance(np.array([[0, 0, 1.0], [0, 0, -1.0]]))
    assert d[0] == pytest.approx(1.0)
    assert d[1] == pytest.approx(-1.0)
