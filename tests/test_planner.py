"""端到端规划测试：直线、尖角、曲率、尖点、零长段与不可行判定。"""

import numpy as np
import pytest

from trajectory_planning.kinematics import InfeasibleTrajectory
from trajectory_planning.planner import build_profile, plan_from_request
from trajectory_planning.profile import sample_at, sample_grid


def test_straight_line_analytic_duration_and_bounds(straight_dynamics):
    # L=3 m，v_max=1，a=d=0.5：加速 2s/1m，制动 2s/1m，巡航 1m/1s -> 5s
    profile, report = build_profile(
        [[0.0, 0.0], [3.0, 0.0]], straight_dynamics
    )
    assert report["passed"]
    np.testing.assert_allclose(profile.total_time, 5.0, rtol=1e-6)
    np.testing.assert_allclose(profile.start_speed, 0.0)
    np.testing.assert_allclose(profile.end_speed, 0.0)
    assert report["max_speed"] <= 1.0 + 1e-7
    assert report["max_accel_numeric"] <= 0.5 + 2e-3
    assert report["max_decel_numeric"] <= 0.5 + 2e-3


def test_straight_line_position_endpoint(straight_dynamics):
    profile, _ = build_profile([[0.0, 0.0], [3.0, 0.0]], straight_dynamics)
    end = sample_at(profile, profile.total_time)
    np.testing.assert_allclose(end["position"], [3.0, 0.0], atol=1e-6)
    start = sample_at(profile, 0.0)
    np.testing.assert_allclose(start["position"], [0.0, 0.0], atol=1e-9)


def test_sharp_corner_stop_node_speed_zero(straight_dynamics):
    profile, report = build_profile(
        [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0]], straight_dynamics,
        {"strategy": "stop"},
    )
    assert report["passed"]
    np.testing.assert_allclose(profile.node_speeds[1], 0.0)
    assert profile.corner_info[0]["forced_stop_reason"] == "stop_strategy"


def test_sharp_corner_velocity_touches_zero_at_corner(straight_dynamics):
    profile, _ = build_profile(
        [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0]], straight_dynamics,
        {"strategy": "stop"},
    )
    # 在拐角附近采样，速度最小值应为 0
    grid = sample_grid(profile, 0.005)
    assert grid["speed"].min() <= 1e-6


def test_curvature_corner_speed_bound():
    # R=0.5, a_lat=0.6 -> v_corner = sqrt(0.6*0.5) = sqrt(0.3)
    dynamics = {
        "v_max": 1.5, "a_max": 0.8, "d_max": 0.8,
        "a_lat_max": 0.6, "v_start": 0.0, "v_end": 0.0,
    }
    profile, report = build_profile(
        [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0]],
        dynamics, {"strategy": "curvature", "radius": 0.5},
    )
    assert report["passed"]
    expected = np.sqrt(0.6 * 0.5)
    np.testing.assert_allclose(profile.node_speeds[1], expected, rtol=1e-6)
    assert profile.node_speeds[1] < 1.5


def test_curvature_strategy_requires_lateral_accel():
    dynamics = {"v_max": 1.0, "a_max": 0.5, "d_max": 0.5}
    with pytest.raises(ValueError, match="a_lat_max"):
        build_profile(
            [[0, 0], [1, 0], [1, 1]], dynamics,
            {"strategy": "curvature", "radius": 0.5},
        )


def test_curvature_strategy_requires_radius():
    dynamics = {"v_max": 1.0, "a_max": 0.5, "d_max": 0.5, "a_lat_max": 0.5}
    with pytest.raises(ValueError, match="显式曲率"):
        build_profile(
            [[0, 0], [1, 0], [1, 1]], dynamics,
            {"strategy": "curvature"},
        )


def test_cusp_forced_stop_even_under_curvature():
    # 180 度折返：即使 curvature 策略也强制停车
    dynamics = {
        "v_max": 1.0, "a_max": 0.5, "d_max": 0.5,
        "a_lat_max": 2.0, "v_start": 0.0, "v_end": 0.0,
    }
    profile, _ = build_profile(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 0.0]],
        dynamics, {"strategy": "curvature", "radius": 0.1,
                   "cusp_angle_deg": 170.0},
    )
    np.testing.assert_allclose(profile.node_speeds[1], 0.0)
    assert profile.corner_info[0]["forced_stop_reason"] == "cusp"


def test_explicit_stop_node():
    dynamics = {"v_max": 1.0, "a_max": 0.5, "d_max": 0.5}
    # 直行路径中间节点，曲率策略本可高速通过，但显式要求停
    profile, _ = build_profile(
        [[0.0, 0.0], [2.0, 0.0], [4.0, 0.0]],
        dynamics, {"strategy": "curvature", "radius": 10.0,
                   "stop_nodes": [1]},
    )
    np.testing.assert_allclose(profile.node_speeds[1], 0.0)


def test_nonzero_start_end_speeds():
    dynamics = {
        "v_max": 2.0, "a_max": 1.0, "d_max": 1.0,
        "v_start": 1.0, "v_end": 1.0,
    }
    profile, report = build_profile([[0, 0], [10, 0]], dynamics)
    assert report["passed"]
    np.testing.assert_allclose(profile.start_speed, 1.0)
    np.testing.assert_allclose(profile.end_speed, 1.0)


def test_infeasible_start_speed_detected():
    # 3 m/s 起步，0.1 m 后要求停止，d=0.5 -> 不可行
    dynamics = {
        "v_max": 5.0, "a_max": 0.5, "d_max": 0.5,
        "v_start": 3.0, "v_end": 0.0,
    }
    with pytest.raises(InfeasibleTrajectory, match="起点速度"):
        build_profile([[0, 0], [0.1, 0]], dynamics)


def test_zero_length_segment_end_to_end():
    dynamics = {
        "v_max": 1.0, "a_max": 0.5, "d_max": 0.5,
        "v_start": 0.0, "v_end": 0.0,
    }
    profile, report = build_profile(
        [[0.0, 0.0], [1.0, 0.0], [1.0, 0.0], [2.0, 0.0], [2.0, 1.0]],
        dynamics, {"strategy": "stop"},
    )
    assert report["passed"]
    assert profile.polyline.removed_duplicates == 1
    assert profile.total_time > 0.0
    grid = sample_grid(profile, 0.01)
    assert np.all(np.isfinite(grid["speed"]))


def test_monotonic_time_and_continuous_position(straight_dynamics):
    profile, _ = build_profile(
        [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0]], straight_dynamics
    )
    grid = sample_grid(profile, 0.01)
    assert np.all(np.diff(grid["t"]) > 0)
    gaps = np.linalg.norm(np.diff(grid["position"], axis=0), axis=1)
    assert gaps.max() < 0.1  # 无位置跳变


def test_plan_from_request_serializable(straight_dynamics):
    result = plan_from_request({
        "points": [[0, 0], [3, 0]],
        "dynamics": straight_dynamics,
        "corner": {"strategy": "stop"},
    })
    assert result["status"] == "ok"
    assert result["summary"]["total_time"] > 0
    assert result["validation"]["passed"]
    import json
    json.dumps(result)  # 必须可序列化
