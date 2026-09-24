"""几何原语单元测试。"""

import numpy as np

from app.geometry import (
    menger_curvature_sq,
    rect_sdf,
    rect_sdf_grad,
    segment_rect_penetration,
    validate_trajectory,
)
from app.preprocess import (
    characteristic_length,
    expand_with_map,
    merge_consecutive_duplicates,
)

RECT = (0.0, 0.0, 4.0, 2.0)


def test_rect_sdf_exterior_regions():
    pts = np.array([
        [6.0, 1.0],   # 右侧带：距离 2
        [2.0, 4.0],   # 上方带：距离 2
        [6.0, 4.0],   # 对角：2√2
        [2.0, 1.0],   # 内部
        [4.0, 1.0],   # 右边界上：0
    ])
    s = rect_sdf(pts, RECT)
    assert np.isclose(s[0], 2.0)
    assert np.isclose(s[1], 2.0)
    assert np.isclose(s[2], np.hypot(2, 2))
    assert s[3] < 0.0
    assert np.isclose(s[3], -1.0)
    assert np.isclose(s[4], 0.0, atol=1e-12)


def test_rect_sdf_grad_matches_finite_difference():
    rng = np.random.default_rng(0)
    for _ in range(200):
        p = rng.uniform(-3, 7, size=2)
        g = rect_sdf_grad(p, RECT)
        eps = 1e-7
        num = np.array([
            (rect_sdf((p + np.array([eps, 0])).reshape(1, 2), RECT)[0]
             - rect_sdf((p - np.array([eps, 0])).reshape(1, 2), RECT)[0]) / (2 * eps),
            (rect_sdf((p + np.array([0, eps])).reshape(1, 2), RECT)[0]
             - rect_sdf((p - np.array([0, eps])).reshape(1, 2), RECT)[0]) / (2 * eps),
        ])
        # 避开非光滑面（中线/延长线）附近的抽样点。
        if np.abs(p[0] - 2.0) > 5e-5 and np.abs(p[1] - 1.0) > 5e-5:
            assert np.allclose(g, num, atol=1e-5), (p, g, num)


def test_segment_penetration_exterior():
    pen, t = segment_rect_penetration(np.array([-2.0, 1.0]), np.array([-1.0, 1.0]), RECT)
    assert pen == 1.0 and t == -1.0


def test_segment_penetration_tangent_is_not_collision():
    # 与顶边相切，不算碰撞。
    pen, t = segment_rect_penetration(np.array([1.0, 2.0]), np.array([3.0, 2.0]), RECT)
    assert pen == 0.0


def test_segment_penetration_interior():
    # 水平穿过矩形内部，最深 0.5。
    pen, t = segment_rect_penetration(np.array([-1.0, 0.5]), np.array([5.0, 0.5]), RECT)
    assert np.isclose(pen, 0.5)
    assert 0.0 <= t <= 1.0


def test_segment_penetration_corner_diagonal():
    # 从角点外斜穿到内部。
    pen, _ = segment_rect_penetration(np.array([-1.0, -1.0]), np.array([2.0, 1.0]), RECT)
    assert pen > 0.0


def test_segment_penetration_matches_dense_scan():
    """解析侵入深度与 10 万点密集扫描结果一致（验证没有采样近似漏洞）。"""
    rng = np.random.default_rng(1)
    for _ in range(50):
        a = rng.uniform(-3, 5, size=2)
        b = rng.uniform(-3, 5, size=2)
        pen, t = segment_rect_penetration(a, b, RECT)
        us = np.linspace(0, 1, 100_001)
        pts = a[None, :] + us[:, None] * (b - a)[None, :]
        s_min = float(rect_sdf(pts, RECT).min())
        expected = max(0.0, -s_min) if s_min < 0 else None
        if expected is None:
            assert pen >= 0.0  # 外部：值为间隙，仅校验符号
        else:
            seg_len = float(np.linalg.norm(b - a))
            # 10 万点扫描的参数分辨率 1e-5，位置误差 ~ seg_len*1e-5。
            assert np.isclose(pen, expected, atol=1e-5 * seg_len + 1e-8), (
                a, b, pen, expected
            )


def test_menger_curvature_known_values():
    # 共线 -> 0
    assert menger_curvature_sq([0, 0], [1, 0], [2, 0]) == 0.0
    # 单位圆上三点 (1,0),(0,1),(-1,0) -> κ = 1
    k = np.sqrt(menger_curvature_sq([1, 0], [0, 1], [-1, 0]))
    assert np.isclose(k, 1.0)
    # 边长 2 的等边三角形，外接圆半径 R = 2/√3 -> κ = √3/2
    k = np.sqrt(menger_curvature_sq([-1, 0], [1, 0], [0, np.sqrt(3)]))
    assert np.isclose(k, np.sqrt(3) / 2)


def test_validate_trajectory_detects_mid_segment_collision():
    """控制点全在障碍外，但线段中点穿过障碍：必须检出（不能只查控制点）。"""
    pts = np.array([
        [-1.0, -1.0],   # 外
        [5.0, 3.0],     # 外（对角外）
        [5.0, -1.0],
    ])
    min_clr, events = validate_trajectory(pts, [RECT])
    assert len(events) >= 1
    assert min_clr < 0.0
    assert all(e.segment_index in (0, 1) and e.obstacle_index == 0 for e in events)


def test_validate_trajectory_safety_margin():
    pts = np.array([[2.0, 2.1], [2.0, 3.0]])  # 间隙 0.1
    _, events0 = validate_trajectory(pts, [RECT], safety_margin=0.0)
    _, events1 = validate_trajectory(pts, [RECT], safety_margin=0.2)
    assert events0 == []
    assert len(events1) == 1


def test_dedupe_consecutive_and_restore():
    pts = [[0, 0], [0, 0], [1, 1], [1, 1], [1, 1], [2, 0]]
    r = merge_consecutive_duplicates(pts, tol=1e-9)
    assert r.unique_count == 3
    assert r.index_map == [0, 0, 1, 1, 1, 2]
    restored = expand_with_map(r.points + 10, r.index_map)
    assert np.allclose(restored[0], restored[1])
    assert np.allclose(restored[2:5], restored[2])
    assert len(restored) == 6


def test_dedupe_keeps_nonconsecutive_revisit():
    pts = [[0, 0], [1, 1], [0, 0]]  # 折返：非连续重复必须保留
    r = merge_consecutive_duplicates(pts, tol=1e-9)
    assert r.unique_count == 3


def test_characteristic_length():
    assert np.isclose(characteristic_length([[0, 0], [3, 4]]), 5.0)
    assert characteristic_length([[7, 7]]) == 1.0
