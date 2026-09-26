"""SE2 数学：角度归一化、复合/求逆、残差与解析雅可比。"""

from __future__ import annotations

import numpy as np

from pose_graph_optimizer.se2 import (
    compose_pose,
    edge_jacobians,
    invert_pose,
    pose_error,
    rotation_matrix,
    wrap_angle,
)


def test_wrap_angle_basic_and_identity():
    assert wrap_angle(0.0) == 0.0
    assert abs(wrap_angle(3.2)) < np.pi
    assert abs(wrap_angle(-3.2)) <= np.pi
    # 相差 2π 整数倍必须归一化为同一角度
    assert abs(wrap_angle(7.3) - wrap_angle(7.3 + 6 * np.pi)) < 1e-12
    assert abs(wrap_angle(-np.pi) - np.pi) < 1e-12  # 定义区间 (-pi, pi]


def test_rotation_matrix_orthogonal():
    r = rotation_matrix(0.7)
    assert np.allclose(r @ r.T, np.eye(2))
    assert np.isclose(np.linalg.det(r), 1.0)


def test_compose_invert_roundtrip_and_origin():
    p = np.array([1.3, -0.8, 1.7])
    identity = compose_pose(invert_pose(p), p)
    assert np.allclose(identity, [0.0, 0.0, 0.0], atol=1e-12)

    # 角度自动归一化
    q = compose_pose(np.array([0.0, 0.0, 3.0]), np.array([0.0, 0.0, 3.0]))
    assert abs(q[2]) <= np.pi


def test_pose_error_zero_for_consistent_poses():
    pi = np.array([1.0, 2.0, 0.5])
    z = np.array([0.6, 0.2, 0.3])
    pj = compose_pose(pi, z)
    e = pose_error(pi, pj, z)
    assert np.allclose(e, 0.0, atol=1e-12)


def test_pose_error_angle_wraps_across_pi():
    # 估计角为 -(pi+0.02)，与测量 pi-0.02 是同一方向（相差 2π）。
    # 不做 wrap 会得到 -2π 的伪误差；wrap 后应接近 0。
    pi = np.array([0.0, 0.0, 0.0])
    pj = np.array([0.0, 0.0, -(np.pi + 0.02)])
    z = np.array([0.0, 0.0, np.pi - 0.02])
    e = pose_error(pi, pj, z)
    assert abs(e[2]) < 1e-9
    assert abs(e[2]) < 1.0  # 绝不能出现接近 2π 的角度残差


def test_edge_jacobians_match_finite_differences():
    rng = np.random.default_rng(0)
    pi = np.array([0.5, -0.3, 0.9])
    pj = np.array([1.4, 0.2, -1.2])
    z = np.array([0.7, 0.6, 2.5])  # 大角度测量，检验跨 π 附近的雅可比

    a_i, b_j = edge_jacobians(pi, pj, z)
    eps = 1e-6

    def fd(pose: np.ndarray, which: str) -> np.ndarray:
        num = np.zeros((3, 3))
        for k in range(3):
            p_lo = pose.copy()
            p_hi = pose.copy()
            p_lo[k] -= eps
            p_hi[k] += eps
            if which == "i":
                e_lo = pose_error(p_lo, pj, z)
                e_hi = pose_error(p_hi, pj, z)
            else:
                e_lo = pose_error(pi, p_lo, z)
                e_hi = pose_error(pi, p_hi, z)
            num[:, k] = (e_hi - e_lo) / (2 * eps)
        return num

    assert np.allclose(a_i, fd(pi, "i"), atol=1e-6)
    assert np.allclose(b_j, fd(pj, "j"), atol=1e-6)


def test_jacobian_rotation_blocks_opposite_signs():
    # 残差对两节点平移的导数互为负号（刚体一致性）
    pi = np.array([0.0, 0.0, 0.3])
    pj = np.array([1.0, 0.0, 0.4])
    z = np.array([0.9, 0.0, 0.1])
    a_i, b_j = edge_jacobians(pi, pj, z)
    assert np.allclose(a_i[:2, :2], -b_j[:2, :2], atol=1e-12)
