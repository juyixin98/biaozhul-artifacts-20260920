"""端到端流水线测试：JSON 请求 → 位姿 + 诊断。

直行、原地旋转两个场景用手算解析解核对；
回绕、倒车、丢样场景核对诊断标记与位姿合理性。
"""

import math

import numpy as np
import pytest

from odometry.pipeline import run_odometry

ROBOT = {
    "wheel_diameter_left": 0.1,
    "wheel_diameter_right": 0.1,
    "track_width": 0.4,
    "ticks_per_revolution": 1000,
    "encoder_modulus": 1000,
}

# 每 tick 位移 = pi * 0.1 / 1000 = pi * 1e-4 m
M_PER_TICK = math.pi * 1e-4


def _request(samples, **extra):
    req = {"robot": ROBOT, "samples": samples}
    req.update(extra)
    return req


def test_straight_line_end_to_end():
    # 每步两轮 +100 tick，10 步 → 每步 0.01*pi m，总 x = 0.1*pi
    samples = [
        {"t": i * 0.1, "left": 100 * i, "right": 100 * i} for i in range(11)
    ]
    result = run_odometry(_request(samples))
    final = result["summary"]["final_pose"]
    assert final["x"] == pytest.approx(0.1 * math.pi, abs=1e-9)
    assert final["y"] == pytest.approx(0.0, abs=1e-12)
    assert final["theta"] == pytest.approx(0.0, abs=1e-12)
    assert result["summary"]["flagged_samples"] == 0
    assert len(result["poses"]) == 11


def test_rotation_in_place_end_to_end():
    # 左 -50 / 右 +50 tick 每步，40 步
    # 每步 dtheta = (100 * pi*1e-4) / 0.4 = 0.025*pi → 总计 pi
    samples = [
        {"t": i * 0.1, "left": -50 * i, "right": 50 * i} for i in range(41)
    ]
    result = run_odometry(_request(samples))
    final = result["summary"]["final_pose"]
    assert abs(abs(final["theta"]) - math.pi) < 1e-9
    assert final["x"] == pytest.approx(0.0, abs=1e-9)
    assert final["y"] == pytest.approx(0.0, abs=1e-9)


def test_counter_wrap_end_to_end():
    # 模 1000：990 → 10 → 30，真实每步 +20 tick，共 +40 tick
    samples = [
        {"t": 0.0, "left": 990, "right": 990},
        {"t": 0.1, "left": 10, "right": 10},
        {"t": 0.2, "left": 30, "right": 30},
    ]
    result = run_odometry(_request(samples))
    final = result["summary"]["final_pose"]
    assert final["x"] == pytest.approx(40 * M_PER_TICK, abs=1e-12)
    flags = [set(d["flags"]) for d in result["diagnostics"]]
    assert "counter_wrap_left" in flags[1]
    assert "counter_wrap_right" in flags[1]
    assert not flags[2]


def test_reverse_end_to_end():
    # 倒车 5 步，每步 -100 tick → x = -0.05*pi
    samples = [
        {"t": i * 0.1, "left": -100 * i, "right": -100 * i} for i in range(6)
    ]
    result = run_odometry(_request(samples))
    final = result["summary"]["final_pose"]
    assert final["x"] == pytest.approx(-0.05 * math.pi, abs=1e-12)


def test_dropped_sample_time_gap_flagged():
    # 第 3 个样本时间跳到 1.0（间隔 0.8 s > 默认 max_dt 0.5）
    samples = [
        {"t": 0.0, "left": 0, "right": 0},
        {"t": 0.1, "left": 100, "right": 100},
        {"t": 1.0, "left": 200, "right": 200},
    ]
    result = run_odometry(_request(samples))
    flags = [set(d["flags"]) for d in result["diagnostics"]]
    assert "time_gap" in flags[2]
    # 丢样不丢计数：位移仍按全部增量积分
    assert result["summary"]["final_pose"]["x"] == pytest.approx(
        200 * M_PER_TICK, abs=1e-12
    )


def test_velocity_spike_flagged():
    # 无回绕计数器：dt=0.1 s 内跳变 50000 tick ≈ 15.7 m → 远超默认 5 m/s
    robot = dict(ROBOT, encoder_modulus=None)
    samples = [
        {"t": 0.0, "left": 0, "right": 0},
        {"t": 0.1, "left": 50000, "right": 50000},
    ]
    result = run_odometry({"robot": robot, "samples": samples})
    flags = [set(d["flags"]) for d in result["diagnostics"]]
    assert "velocity_spike" in flags[1]


def test_initial_pose_from_request():
    samples = [
        {"t": 0.0, "left": 0, "right": 0},
        {"t": 0.1, "left": 100, "right": 100},
    ]
    result = run_odometry(
        _request(samples, initial_pose={"x": 1.0, "y": 0.0, "theta": 0.0})
    )
    assert result["poses"][0]["x"] == pytest.approx(1.0)
    assert result["summary"]["final_pose"]["x"] == pytest.approx(
        1.0 + 100 * M_PER_TICK, abs=1e-12
    )


def test_unequal_wheel_diameters():
    # 右轮直径是左轮两倍、同样 tick 增量 → 右轮走得更远 → 向左偏航（theta > 0）
    robot = dict(ROBOT, wheel_diameter_right=0.2)
    samples = [
        {"t": 0.0, "left": 0, "right": 0},
        {"t": 0.1, "left": 100, "right": 100},
    ]
    result = run_odometry({"robot": robot, "samples": samples})
    assert result["summary"]["final_pose"]["theta"] > 0.0


@pytest.mark.parametrize(
    "bad_request,fragment",
    [
        ({"samples": [{"t": 0, "left": 0, "right": 0}]}, "robot"),
        ({"robot": ROBOT, "samples": []}, "samples"),
        ({"robot": ROBOT, "samples": [{"t": 0.0, "left": 0}]}, "right"),
        (
            {"robot": ROBOT, "samples": [{"t": 0.0, "left": 0, "right": "x"}]},
            "数值",
        ),
        (
            {
                "robot": ROBOT,
                "samples": [{"t": float("nan"), "left": 0, "right": 0}],
            },
            "有限",
        ),
        (
            {
                "robot": dict(ROBOT, track_width=-1),
                "samples": [{"t": 0.0, "left": 0, "right": 0}],
            },
            "轴距",
        ),
    ],
)
def test_invalid_requests_rejected(bad_request, fragment):
    with pytest.raises(ValueError, match=fragment):
        run_odometry(bad_request)


def test_synthetic_roundtrip_straight():
    # 用合成发生器模拟 0.5 m/s × 2 s，量化误差内应回到 x ≈ 1.0 m
    from odometry.config import RobotParams
    from odometry.synthetic import simulate_samples

    params = RobotParams.from_dict(ROBOT)
    samples = simulate_samples(params, [(0.5, 0.0, 2.0)], dt=0.05)
    result = run_odometry({"robot": ROBOT, "samples": samples})
    final = result["summary"]["final_pose"]
    # 量化步长 pi*1e-4 m，64 步累积误差远小于 1 mm
    assert final["x"] == pytest.approx(1.0, abs=1e-3)
    assert final["y"] == pytest.approx(0.0, abs=1e-3)
