"""主流程集成测试：回绕、倒车、丢样、跳变诊断、打滑局限性。"""

import math

import pytest

from diff_odom.config import RobotParams
from diff_odom.pipeline import run_odometry

PARAMS = RobotParams(
    wheel_diameter_m=0.1,
    track_width_m=0.5,
    ticks_per_rev=1000,
    encoder_min=0,
    encoder_max=65535,
    max_linear_velocity_mps=3.0,
    max_angular_velocity_rps=12.0,
    max_sample_period_s=0.5,
)
MPT = math.pi * 1e-4


def run(counts, dt=0.1, params=PARAMS):
    """由 (左, 右) 计数序列构造等间隔采样并积分。"""
    timestamps = [i * dt for i in range(len(counts))]
    left = [c[0] for c in counts]
    right = [c[1] for c in counts]
    return run_odometry(params, timestamps, left, right)


class TestStraightAndReverse:
    def test_straight_10_steps(self):
        counts = [(i * 1000, i * 1000) for i in range(11)]
        result = run(counts)
        final = result["summary"]["final_pose"]
        assert final["x_m"] == pytest.approx(10 * 1000 * MPT, abs=1e-9)
        assert final["y_m"] == pytest.approx(0.0, abs=1e-9)
        assert final["theta_rad"] == pytest.approx(0.0, abs=1e-9)
        assert result["summary"]["diagnostic_counts"]["error"] == 0

    def test_reverse(self):
        # 无符号计数器倒车时向下回绕：-i*500 (mod 65536)
        counts = [((-i * 500) % 65536, (-i * 500) % 65536) for i in range(5)]
        result = run(counts)
        final = result["summary"]["final_pose"]
        assert final["x_m"] == pytest.approx(-4 * 500 * MPT, abs=1e-9)
        assert result["summary"]["total_distance_m"] == pytest.approx(
            4 * 500 * MPT, abs=1e-9
        )


class TestWraparound:
    def test_forward_wrap_in_pipeline(self):
        params = RobotParams(
            wheel_diameter_m=0.1,
            track_width_m=0.5,
            ticks_per_rev=1000,
            encoder_min=0,
            encoder_max=1023,
        )
        # 1000 -> 30 (mod 1024) 表示前进 54 tick
        result = run([(1000, 1000), (30, 30)], params=params)
        step = result["steps"][0]
        assert step["d_left_ticks"] == 54
        assert step["d_right_ticks"] == 54
        wraps = [d for d in result["diagnostics"] if d["type"] == "counter_wrap"]
        assert len(wraps) == 1
        assert wraps[0]["severity"] == "info"

    def test_reverse_wrap_in_pipeline(self):
        params = RobotParams(
            wheel_diameter_m=0.1,
            track_width_m=0.5,
            ticks_per_rev=1000,
            encoder_min=0,
            encoder_max=1023,
        )
        # 30 -> 1000 (mod 1024) 表示倒退 54 tick
        result = run([(30, 30), (1000, 1000)], params=params)
        assert result["steps"][0]["d_left_ticks"] == -54


class TestDiagnostics:
    def test_sample_gap_flagged(self):
        result = run_odometry(
            PARAMS,
            [0.0, 0.1, 1.3, 1.4],  # 1.2s 间隔 > 0.5s 阈值
            [0, 100, 200, 300],
            [0, 100, 200, 300],
        )
        gaps = [d for d in result["diagnostics"] if d["type"] == "sample_gap"]
        assert len(gaps) == 1
        assert gaps[0]["index"] == 2
        assert gaps[0]["severity"] == "warning"

    def test_nonmonotonic_timestamp_flagged(self):
        result = run_odometry(
            PARAMS,
            [0.0, 0.1, 0.05],
            [0, 100, 200],
            [0, 100, 200],
        )
        errors = [
            d
            for d in result["diagnostics"]
            if d["type"] == "timestamp_nonmonotonic"
        ]
        assert len(errors) == 1
        assert errors[0]["severity"] == "error"

    def test_velocity_jump_flagged(self):
        # 一步 20000 tick = 2π m，dt=0.1s -> 约 62.8 m/s，远超 3 m/s 上限
        result = run([(0, 0), (20000, 20000)])
        jumps = [d for d in result["diagnostics"] if d["type"] == "velocity_jump"]
        assert len(jumps) == 1
        assert jumps[0]["severity"] == "warning"

    def test_angular_velocity_jump_flagged(self):
        # 原地一步左 -20000 / 右 +20000 tick -> 角速度约 251 rad/s，远超上限
        result = run([(30000, 30000), (10000, 50000)])
        jumps = [
            d
            for d in result["diagnostics"]
            if d["type"] == "angular_velocity_jump"
        ]
        assert len(jumps) == 1

    def test_clean_run_has_no_warnings(self):
        counts = [(i * 100, i * 100) for i in range(20)]
        result = run(counts)
        assert result["summary"]["diagnostic_counts"]["warning"] == 0
        assert result["summary"]["diagnostic_counts"]["error"] == 0


class TestSlipLimitation:
    def test_symmetric_slip_is_undetectable(self):
        """局限性刻画：两轮等量打滑时里程计无感知。

        真实车轮走了 1000 tick 等效距离，但打滑导致编码器只计 600。
        里程计只能按 600 积分，且不会产生任何诊断——
        该测试固化这一已知局限，防止误以为库能检测所有打滑。
        """
        counts = [(0, 0), (600, 600)]
        result = run(counts)
        final = result["summary"]["final_pose"]
        assert final["x_m"] == pytest.approx(600 * MPT, abs=1e-12)
        assert result["summary"]["diagnostic_counts"]["warning"] == 0
        assert result["summary"]["diagnostic_counts"]["error"] == 0


class TestInputValidation:
    def test_mismatched_lengths_raise(self):
        with pytest.raises(ValueError):
            run_odometry(PARAMS, [0.0, 0.1], [0, 1], [0])

    def test_empty_samples_raise(self):
        with pytest.raises(ValueError):
            run_odometry(PARAMS, [], [], [])

    def test_invalid_params_raise(self):
        with pytest.raises(ValueError):
            RobotParams(
                wheel_diameter_m=-0.1, track_width_m=0.5, ticks_per_rev=1000
            )
