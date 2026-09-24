"""投影模型与解析雅可比的正确性测试（有限差分对照）。"""

import numpy as np

from ba.lie import exp_so3, log_so3, skew
from ba.problem import CameraPose, Intrinsics
from ba.projection import project, projection_jacobians


def test_exp_log_roundtrip():
    w = np.array([0.1, -0.2, 0.3])
    np.testing.assert_allclose(log_so3(exp_so3(w)), w, atol=1e-10)
    np.testing.assert_allclose(exp_so3(np.zeros(3)), np.eye(3), atol=1e-12)


def test_projection_known_point():
    # 相机在原点看前方 (0,0,5)：投影到主点
    intr = Intrinsics(fx=500.0, fy=500.0, cx=320.0, cy=240.0)
    pose = CameraPose(R=np.eye(3), t=np.zeros(3))
    uv, pc = project(intr, pose, np.array([0.0, 0.0, 5.0]))
    np.testing.assert_allclose(uv, [320.0, 240.0], atol=1e-10)

    # 平移点 (1,1,5)：u = 500*1/5 + 320
    uv, _ = project(intr, pose, np.array([1.0, 1.0, 5.0]))
    np.testing.assert_allclose(uv, [420.0, 340.0], atol=1e-10)


def test_jacobians_finite_difference():
    rng = np.random.default_rng(42)
    intr = Intrinsics(fx=500.0, fy=500.0, cx=320.0, cy=240.0)
    pose = CameraPose(R=exp_so3(np.array([0.1, -0.05, 0.2])), t=np.array([-0.3, 0.1, 0.0]))
    X = np.array([0.4, -0.2, 6.0])
    J_cam, J_pt = projection_jacobians(intr, pose, X)

    eps = 1e-6

    # 对相机增量 (dr, dt) 的有限差分
    J_cam_fd = np.zeros((2, 6))
    for k in range(6):
        d = np.zeros(6)
        d[k] = eps
        R_p = exp_so3(d[:3]) @ pose.R
        t_p = pose.t + d[3:]
        u_plus, _ = project(intr, CameraPose(R_p, t_p), X)
        d_m = d.copy()
        d_m[k] = -eps
        R_m = exp_so3(d_m[:3]) @ pose.R
        t_m = pose.t + d_m[3:]
        u_minus, _ = project(intr, CameraPose(R_m, t_m), X)
        J_cam_fd[:, k] = (u_plus - u_minus) / (2 * eps)
    np.testing.assert_allclose(J_cam, J_cam_fd, atol=1e-6)

    # 对三维点的有限差分
    J_pt_fd = np.zeros((2, 3))
    for k in range(3):
        d = np.zeros(3)
        d[k] = eps
        u_plus, _ = project(intr, pose, X + d)
        u_minus, _ = project(intr, pose, X - d)
        J_pt_fd[:, k] = (u_plus - u_minus) / (2 * eps)
    np.testing.assert_allclose(J_pt, J_pt_fd, atol=1e-6)
