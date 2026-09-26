"""圆弧积分器的手算验收测试。

三个基准场景（直行 / 原地旋转 / 圆弧）均有解析解，逐一核对。
"""

import numpy as np
import pytest

from odometry.integrate import Pose2D, integrate_differential, wheel_distances


def test_straight_line_hand_calc():
    # 每步两轮各前进 0.1 m，10 步 → x = 1.0, y = 0, theta = 0
    ds = np.zeros(11)
    ds[1:] = 0.1
    poses = integrate_differential(ds, ds.copy(), track_width=0.4)
    assert poses[-1, 0] == pytest.approx(1.0, abs=1e-12)
    assert poses[-1, 1] == pytest.approx(0.0, abs=1e-12)
    assert poses[-1, 2] == pytest.approx(0.0, abs=1e-12)


def test_rotation_in_place_hand_calc():
    # 左轮 -0.1、右轮 +0.1，L=0.4 → 每步 dtheta = 0.5 rad，2 步 → theta = 1.0
    # 中心弧长恒为 0 → x, y 不动
    n = 3
    ds_left = np.zeros(n)
    ds_right = np.zeros(n)
    ds_left[1:] = -0.1
    ds_right[1:] = 0.1
    poses = integrate_differential(ds_left, ds_right, track_width=0.4)
    assert poses[-1, 2] == pytest.approx(1.0, abs=1e-12)
    assert poses[-1, 0] == pytest.approx(0.0, abs=1e-12)
    assert poses[-1, 1] == pytest.approx(0.0, abs=1e-12)


def test_arc_quarter_circle_hand_calc():
    # 半径 R=1 m 的四分之一圆：theta 从 0 转到 pi/2
    # 解析终点: x = R*sin(pi/2) = 1, y = R*(1-cos(pi/2)) = 1
    # 恒定曲率下圆弧积分与步长无关，应精确到浮点误差
    radius, track, steps = 1.0, 0.4, 100
    dtheta = (np.pi / 2.0) / steps
    ds_left = np.full(steps + 1, (radius - track / 2.0) * dtheta)
    ds_right = np.full(steps + 1, (radius + track / 2.0) * dtheta)
    ds_left[0] = ds_right[0] = 0.0
    poses = integrate_differential(ds_left, ds_right, track_width=track)
    assert poses[-1, 0] == pytest.approx(1.0, abs=1e-10)
    assert poses[-1, 1] == pytest.approx(1.0, abs=1e-10)
    assert poses[-1, 2] == pytest.approx(np.pi / 2.0, abs=1e-10)


def test_reverse_motion():
    # 倒车：两轮各 -0.1 m/步，10 步 → x = -1.0
    ds = np.zeros(11)
    ds[1:] = -0.1
    poses = integrate_differential(ds, ds.copy(), track_width=0.4)
    assert poses[-1, 0] == pytest.approx(-1.0, abs=1e-12)
    assert poses[-1, 2] == pytest.approx(0.0, abs=1e-12)


def test_initial_pose_respected():
    ds = np.array([0.0, 0.1])
    poses = integrate_differential(
        ds, ds.copy(), track_width=0.4, initial_pose=Pose2D(1.0, 2.0, np.pi / 2)
    )
    # 朝 +y 方向前进 0.1
    assert poses[-1, 0] == pytest.approx(1.0, abs=1e-12)
    assert poses[-1, 1] == pytest.approx(2.1, abs=1e-12)
    assert poses[-1, 2] == pytest.approx(np.pi / 2, abs=1e-12)


def test_theta_normalized_to_minus_pi_pi():
    # 原地累计旋转超过 pi 后应归一化到 (-pi, pi]
    n = 41
    ds_left = np.zeros(n)
    ds_right = np.zeros(n)
    ds_left[1:] = -0.1
    ds_right[1:] = 0.1
    poses = integrate_differential(ds_left, ds_right, track_width=0.4)
    assert np.all(poses[:, 2] > -np.pi) and np.all(poses[:, 2] <= np.pi)


def test_wheel_distances_conversion():
    # D=0.1, tpr=1000 → 每 tick = pi*1e-4 m；1000 tick = 0.1*pi m
    ds_left, ds_right = wheel_distances(
        np.array([0.0, 1000.0]), np.array([0.0, -500.0]), 0.1, 0.1, 1000.0
    )
    assert ds_left[1] == pytest.approx(0.1 * np.pi)
    assert ds_right[1] == pytest.approx(-0.05 * np.pi)


def test_shape_mismatch_rejected():
    with pytest.raises(ValueError):
        integrate_differential(np.zeros(3), np.zeros(4), track_width=0.4)
