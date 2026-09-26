"""积分器手算验收测试：直行、原地旋转、圆弧、倒车。

测试机器人参数：
    轮径 0.1 m -> 周长 0.1π m；每转 1000 tick -> 1 tick = 1e-4·π m
    轴距 0.5 m
"""

import math

import pytest

from diff_odom.integrator import arc_step, wrap_angle

TRACK = 0.5
MPT = math.pi * 1e-4  # meters per tick


class TestStraightLine:
    def test_forward_1000_ticks(self):
        # 左右各 +1000 tick -> 各轮 0.1π m，直行 0.1π m
        x, y, theta = arc_step(0, 0, 0, 1000 * MPT, 1000 * MPT, TRACK)
        assert x == pytest.approx(0.1 * math.pi, abs=1e-12)
        assert y == pytest.approx(0.0, abs=1e-12)
        assert theta == pytest.approx(0.0, abs=1e-12)

    def test_reverse_1000_ticks(self):
        # 倒车：左右各 -1000 tick -> x = -0.1π
        x, y, theta = arc_step(0, 0, 0, -1000 * MPT, -1000 * MPT, TRACK)
        assert x == pytest.approx(-0.1 * math.pi, abs=1e-12)
        assert y == pytest.approx(0.0, abs=1e-12)
        assert theta == pytest.approx(0.0, abs=1e-12)

    def test_straight_at_45_degrees(self):
        # 朝 45° 方向直行 0.1π m
        x, y, theta = arc_step(
            0, 0, math.pi / 4, 1000 * MPT, 1000 * MPT, TRACK
        )
        expected = 0.1 * math.pi / math.sqrt(2)
        assert x == pytest.approx(expected, abs=1e-12)
        assert y == pytest.approx(expected, abs=1e-12)
        assert theta == pytest.approx(math.pi / 4, abs=1e-12)


class TestRotateInPlace:
    def test_quarter_turn_ccw(self):
        # 左 -1250 / 右 +1250 tick：dθ = (0.125π+0.125π)/0.5 = π/2
        x, y, theta = arc_step(0, 0, 0, -1250 * MPT, 1250 * MPT, TRACK)
        assert x == pytest.approx(0.0, abs=1e-12)
        assert y == pytest.approx(0.0, abs=1e-12)
        assert theta == pytest.approx(math.pi / 2, abs=1e-12)

    def test_full_turn_returns_to_zero(self):
        # 每步转 π/2，4 步累计 2π 后角度归一化回 0，位置不变
        x = y = theta = 0.0
        for _ in range(4):
            x, y, theta = arc_step(0, 0, theta, -1250 * MPT, 1250 * MPT, TRACK)
        assert x == pytest.approx(0.0, abs=1e-9)
        assert y == pytest.approx(0.0, abs=1e-9)
        assert theta == pytest.approx(0.0, abs=1e-9)


class TestArc:
    def test_quarter_circle_radius_1m(self):
        # 左 3750 / 右 6250 tick：
        #   dL=0.375π, dR=0.625π -> ds=0.5π, dθ=0.25π/0.5=π/2, R=ds/dθ=1 m
        # 四分之一圆后：x = R·sin(π/2) = 1, y = R·(1-cos(π/2)) = 1, θ=π/2
        x, y, theta = arc_step(0, 0, 0, 3750 * MPT, 6250 * MPT, TRACK)
        assert x == pytest.approx(1.0, abs=1e-12)
        assert y == pytest.approx(1.0, abs=1e-12)
        assert theta == pytest.approx(math.pi / 2, abs=1e-12)

    def test_arc_composition_equals_single_arc(self):
        # 两段相同 1/4 圆弧 = 一个半圆：终点 (0, 2R, π)
        x = y = theta = 0.0
        for _ in range(2):
            x, y, theta = arc_step(x, y, theta, 3750 * MPT, 6250 * MPT, TRACK)
        assert x == pytest.approx(0.0, abs=1e-9)
        assert y == pytest.approx(2.0, abs=1e-9)
        assert theta == pytest.approx(math.pi, abs=1e-9)


class TestWrapAngle:
    def test_normalizes_to_minus_pi_pi(self):
        assert wrap_angle(3 * math.pi) == pytest.approx(math.pi)
        assert wrap_angle(-3 * math.pi) == pytest.approx(math.pi)
        assert wrap_angle(0.5) == pytest.approx(0.5)
        assert wrap_angle(-math.pi / 2) == pytest.approx(-math.pi / 2)
