"""运动学测试：传播、单段梯形/三角形、零长段、不可行检测。"""

import numpy as np
import pytest

from trajectory_planning.kinematics import (
    InfeasibleTrajectory,
    plan_segment,
    propagate_node_speeds,
)


def test_forward_propagation_caps_by_acceleration():
    # 首速度 0，a=1，长 1m -> 下一节点最多 sqrt(2)
    speeds = propagate_node_speeds(
        np.array([1.0]), np.array([0.0, 10.0]), a_max=1.0, d_max=1.0
    )
    np.testing.assert_allclose(speeds[1], np.sqrt(2.0))


def test_backward_propagation_caps_by_deceleration():
    # 末速度 0，d=1，长 1m -> 前节点最多 sqrt(2)
    speeds = propagate_node_speeds(
        np.array([1.0]), np.array([10.0, 0.0]), a_max=1.0, d_max=1.0
    )
    np.testing.assert_allclose(speeds[0], np.sqrt(2.0))


def test_zero_length_segment_equalizes_speeds_without_division():
    speeds = propagate_node_speeds(
        np.array([0.0, 2.0]), np.array([0.0, 5.0, 0.0]),
        a_max=1.0, d_max=1.0,
    )
    # 零长段两侧节点必须同速
    assert speeds[0] == speeds[1]
    assert np.all(np.isfinite(speeds))


def test_plan_segment_rest_to_rest_triangle_distance_and_time():
    # L=2, v0=v1=0, a=d=1 -> 三角形，峰值 sqrt(2)=2? v_peak^2 = 2L / 2 = L = 2
    plan = plan_segment(0, 2.0, 0.0, 0.0, 1.0, 1.0, v_cap=10.0, t_start=0.0)
    np.testing.assert_allclose(plan.v_peak, np.sqrt(2.0))
    np.testing.assert_allclose(plan.t_coast, 0.0)
    np.testing.assert_allclose(plan.s_acc + plan.s_coast + plan.s_dec, 2.0)
    np.testing.assert_allclose(plan.duration, 2.0 * np.sqrt(2.0))


def test_plan_segment_trapezoid_reaches_cap():
    # 足够长的段应进入巡航
    plan = plan_segment(0, 20.0, 0.0, 0.0, 1.0, 1.0, v_cap=2.0, t_start=0.0)
    assert plan.v_peak == 2.0
    assert plan.t_coast > 0.0
    np.testing.assert_allclose(plan.s_acc, 2.0)
    np.testing.assert_allclose(plan.s_dec, 2.0)
    np.testing.assert_allclose(plan.s_acc + plan.s_coast + plan.s_dec, 20.0)


def test_plan_segment_mismatched_endpoints():
    # 从 1 m/s 加速到 2 m/s
    plan = plan_segment(0, 5.0, 1.0, 2.0, 1.0, 1.0, v_cap=3.0, t_start=0.0)
    assert plan.v_enter == 1.0 and plan.v_exit == 2.0
    np.testing.assert_allclose(plan.s_acc + plan.s_coast + plan.s_dec, 5.0)


def test_plan_segment_zero_length_is_instant():
    plan = plan_segment(3, 0.0, 0.5, 0.5, 1.0, 1.0, v_cap=1.0, t_start=2.0)
    assert plan.duration == 0.0
    assert plan.t_start == 2.0
    # 不产生 NaN
    assert np.isfinite(plan.v_peak)


def test_plan_segment_zero_length_speed_mismatch_raises():
    with pytest.raises(InfeasibleTrajectory, match="零长段"):
        plan_segment(0, 0.0, 1.0, 0.0, 1.0, 1.0, v_cap=2.0, t_start=0.0)


def test_plan_segment_too_short_to_decelerate():
    # 3 m/s 刹停需要 9 m（d=0.5），只有 0.1 m
    with pytest.raises(InfeasibleTrajectory, match="减速度"):
        plan_segment(0, 0.1, 3.0, 0.0, 0.5, 0.5, v_cap=5.0, t_start=0.0)


def test_plan_segment_too_short_to_accelerate():
    with pytest.raises(InfeasibleTrajectory, match="加速度"):
        plan_segment(0, 0.1, 0.0, 3.0, 0.5, 0.5, v_cap=5.0, t_start=0.0)


@pytest.mark.parametrize(
    "a,d", [(0.0, 1.0), (-1.0, 1.0), (1.0, 0.0)]
)
def test_nonpositive_accel_raises(a, d):
    with pytest.raises(InfeasibleTrajectory):
        plan_segment(0, 1.0, 0.0, 0.0, a, d, v_cap=1.0, t_start=0.0)
