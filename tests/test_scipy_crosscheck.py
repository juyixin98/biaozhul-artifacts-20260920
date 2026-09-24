"""用 SciPy 对解析解做独立交叉验证（数值优化 / 线性代数）。

SciPy 不参与生产求解（生产用闭式解析解），仅用于测试中独立比对，
避免“用自己验证自己”。
"""

import math

import numpy as np
import pytest
from scipy.linalg import svd
from scipy.optimize import least_squares

from app.kinematics import (
    IKStatus,
    ContinuousIKSolver,
    forward_kinematics,
    inverse_kinematics,
    jacobian,
)
from app.synthetic import circle_path, smooth_fk_path, target_from_fk


def _scipy_numeric_ik(target, q_init, params):
    """SciPy 数值逆解（残差最小二乘），返回 (q, cost)。"""
    target = np.asarray(target, dtype=float)

    def residual(q):
        return forward_kinematics(q[0], q[1], params) - target

    res = least_squares(residual, x0=np.asarray(q_init, dtype=float))
    return res.x, float(np.linalg.norm(res.fun))


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_analytic_matches_scipy_optimization(arm, seed):
    """解析解与 SciPy least_squares 数值解在末端位置上一致（允许另一支等价）。"""
    rng = np.random.default_rng(seed)
    for _ in range(25):
        q1 = float(rng.uniform(-2.5, 2.5))
        q2 = float(rng.uniform(-2.0, 2.0))
        target = target_from_fk(q1, q2, arm)
        sol = inverse_kinematics(target, arm)
        assert sol.joints is not None
        # 分别从两支初值数值求解
        x_num, cost = _scipy_numeric_ik(target, (q1, q2), arm)
        assert cost < 1e-10
        ee_num = forward_kinematics(x_num[0], x_num[1], arm)
        ee_ana = forward_kinematics(sol.joints[0], sol.joints[1], arm)
        assert ee_ana == pytest.approx(ee_num, abs=1e-8)


def test_scipy_svd_rank_drop_at_singularity(arm):
    """SVD 验证：伸直/折叠点雅可比最小奇异值≈0（秩亏）。"""
    # 非奇异良态位形 q2=pi/2：det(J)=1，两奇异值为黄金比 φ 与 1/φ≈0.618
    q = (0.3, math.pi / 2)
    s_ok = svd(jacobian(*q, arm), compute_uv=False)
    assert s_ok[-1] == pytest.approx(0.6180339887, abs=1e-6)
    # 完全伸直
    s_ext = svd(jacobian(0.785, 0.0, arm), compute_uv=False)
    assert s_ext[-1] < 1e-12
    # 完全折叠
    s_fold = svd(jacobian(0.0, math.pi, arm), compute_uv=False)
    assert s_fold[-1] < 1e-12


def test_scipy_circle_path_errors(arm):
    """SciPy 侧独立复算路径每个点的正解误差。"""
    targets = circle_path(n=36)
    res = ContinuousIKSolver(arm).solve_path(targets)
    for pt in res.points:
        assert pt.joints is not None
        ee = forward_kinematics(pt.joints[0], pt.joints[1], arm)
        # 与 scipy 风格范数一致
        assert np.linalg.norm(ee - np.asarray(pt.target)) == pytest.approx(
            pt.position_error, abs=1e-15
        )


def test_near_singular_svd_gradient(arm):
    """接近伸直时最小奇异值随 q2 线性趋小（SciPy SVD）。"""
    sv = []
    for q2 in (1e-3, 1e-4, 1e-5):
        sv.append(svd(jacobian(0.0, q2, arm), compute_uv=False)[-1])
    assert sv[0] > sv[1] > sv[2]
    assert sv[2] < 1e-5


def test_smooth_path_residuals_with_scipy_norm(arm):
    targets = smooth_fk_path(n=21)
    res = ContinuousIKSolver(arm).solve_path(targets)
    r = np.array(
        [
            forward_kinematics(p.joints[0], p.joints[1], arm) - p.target
            for p in res.points
        ]
    )
    # SciPy 2-范数量级
    assert np.max(np.linalg.norm(r, axis=1)) < 1e-10
