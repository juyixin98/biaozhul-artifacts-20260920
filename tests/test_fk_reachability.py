"""正运动学与可达性预检测试。"""

import numpy as np
import pytest

from app.core.reachability import wrist_reachability
from app.core.robot_model import RobotModel, pose_error, rotation_error


def test_zero_pose_fk_closed_form(robot: RobotModel):
    """零位正解的闭式期望值（由 DH 手工推导）。

    零位末端：p = (a2+a3+d6, 0, d1-d4) = (0.70, 0, 0.18)
    姿态：Rx(α1=π/2) 与末端 α6=0、α4=-π/2/α5=π/2 复合 → diag(1,-1,-1)
    """
    T = robot.fk(np.zeros(6))
    np.testing.assert_allclose(T[:3, 3], [0.70, 0.0, 0.18], atol=1e-12)
    np.testing.assert_allclose(T[:3, :3], np.diag([1.0, -1.0, -1.0]), atol=1e-12)


def test_frames_are_rigid_transforms(robot: RobotModel):
    rng = np.random.default_rng(7)
    for _ in range(20):
        q = rng.uniform(-np.pi, np.pi, 6)
        for T in robot.fk_all(q):
            R = T[:3, :3]
            np.testing.assert_allclose(R.T @ R, np.eye(3), atol=1e-10)
            assert abs(np.linalg.det(R) - 1.0) < 1e-10
            np.testing.assert_allclose(T[3], [0, 0, 0, 1])


def test_fk_periodic_invariance(robot: RobotModel):
    """J6 加 2π，位姿不变。"""
    q = np.array([0.3, -0.4, 0.8, 0.2, 0.5, 0.7])
    q2 = q.copy()
    q2[5] += 2 * np.pi
    np.testing.assert_allclose(robot.fk(q), robot.fk(q2), atol=1e-11)


def test_rotation_error_zero_at_identity_and_antipodal():
    assert np.linalg.norm(rotation_error(np.eye(3), np.eye(3))) < 1e-12
    Rz = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1.0]])
    e = rotation_error(np.eye(3), Rz)
    np.testing.assert_allclose(np.linalg.norm(e), np.pi / 2, atol=1e-9)
    np.testing.assert_allclose(e / np.linalg.norm(e), [0, 0, 1], atol=1e-9)


def test_max_reach_value(robot: RobotModel):
    a2, a3, d4 = robot.a[1], robot.a[2], robot.d[3]
    expected = a2 + np.hypot(a3, d4)
    # 伸直构型 q=(q1, 0, δ, ...)：腕点在肩平面内最远端
    delta = np.arctan2(d4, a3)
    q = np.array([0.3, 0.0, delta, 0.0, 0.0, 0.0])
    wc = robot.fk_all(q)[3][:3, 3]
    radial = np.hypot(wc[0], wc[1])
    zrel = wc[2] - robot.d[0]
    np.testing.assert_allclose(np.hypot(radial, zrel), expected, atol=1e-9)


def test_reachability_boundary(robot: RobotModel):
    a2, a3, d4 = robot.a[1], robot.a[2], robot.d[3]
    rmax = a2 + np.hypot(a3, d4)
    # 肩平面水平最远端（zp=0）
    inside = wrist_reachability(robot, np.array([rmax - 1e-4, 0.0, robot.d[0]]))
    on = wrist_reachability(robot, np.array([rmax, 0.0, robot.d[0]]))
    outside = wrist_reachability(robot, np.array([rmax + 1e-3, 0.0, robot.d[0]]))
    assert inside.reachable and inside.within_limits
    assert on.reachable
    assert not outside.reachable
    # 原点方向（臂折回）可达
    origin_like = wrist_reachability(robot, np.array([0.0, 0.0, robot.d[0]]))
    assert origin_like.reachable


def test_reachability_far_and_near(robot: RobotModel):
    far = wrist_reachability(robot, np.array([10.0, 0.0, robot.d[0]]))
    assert not far.reachable
    assert far.max_reach < far.requested_radius
    # 基轴正上方（r=0 退化方位）不判限位冲突
    axis = wrist_reachability(robot, np.array([0.0, 0.0, robot.d[0] + 0.1]))
    assert axis.reachable and axis.within_limits
