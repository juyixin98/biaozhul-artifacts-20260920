"""JSON 入口端到端测试：加载 examples/ 请求并校验手算结果。"""

import json
import math
import os

import pytest

from diff_odom.json_io import handle_request

EXAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples"
)


def load_example(name):
    with open(os.path.join(EXAMPLES_DIR, name), encoding="utf-8") as f:
        return json.load(f)


class TestStraightExample:
    def test_final_pose(self):
        # 10 步 × 1000 tick × 1e-4·π m/tick = π 米直行
        result = handle_request(load_example("request_straight.json"))
        final = result["summary"]["final_pose"]
        assert final["x_m"] == pytest.approx(math.pi, abs=1e-9)
        assert final["y_m"] == pytest.approx(0.0, abs=1e-9)
        assert final["theta_rad"] == pytest.approx(0.0, abs=1e-9)
        assert result["summary"]["diagnostic_counts"]["error"] == 0


class TestRotateExample:
    def test_final_pose(self):
        # 10 步 × (左-200, 右+200)：总转角 10×400×1e-4·π/0.5 = 0.8π
        result = handle_request(load_example("request_rotate_in_place.json"))
        final = result["summary"]["final_pose"]
        assert final["x_m"] == pytest.approx(0.0, abs=1e-9)
        assert final["y_m"] == pytest.approx(0.0, abs=1e-9)
        assert final["theta_rad"] == pytest.approx(0.8 * math.pi, abs=1e-9)


class TestArcExample:
    def test_final_pose(self):
        # 5 步 × (左750, 右1250)：R=1 m 的 90° 圆弧 -> (1, 1, π/2)
        result = handle_request(load_example("request_arc.json"))
        final = result["summary"]["final_pose"]
        assert final["x_m"] == pytest.approx(1.0, abs=1e-9)
        assert final["y_m"] == pytest.approx(1.0, abs=1e-9)
        assert final["theta_rad"] == pytest.approx(math.pi / 2, abs=1e-9)


class TestWraparoundReverseExample:
    def test_net_displacement(self):
        # 前进 6×50=300 tick，倒车 4×80=320 tick，净 -20 tick
        result = handle_request(load_example("request_wraparound_reverse.json"))
        final = result["summary"]["final_pose"]
        assert final["x_m"] == pytest.approx(-20 * math.pi * 1e-4, abs=1e-9)
        assert result["summary"]["counter_wrap_events"] >= 2
        assert result["summary"]["diagnostic_counts"]["error"] == 0


class TestGapAndJumpExample:
    def test_diagnostics_present(self):
        result = handle_request(load_example("request_gap_and_jump.json"))
        types = {d["type"] for d in result["diagnostics"]}
        assert "sample_gap" in types
        assert "velocity_jump" in types


class TestRequestValidation:
    def test_missing_robot_key(self):
        with pytest.raises(ValueError, match="robot"):
            handle_request({"samples": [{"t": 0, "left": 0, "right": 0}]})

    def test_empty_samples(self):
        with pytest.raises(ValueError, match="samples"):
            handle_request(
                {"robot": {"wheel_diameter_m": 0.1, "track_width_m": 0.5,
                           "ticks_per_rev": 1000}, "samples": []}
            )

    def test_sample_missing_field(self):
        with pytest.raises(ValueError, match="left"):
            handle_request(
                {"robot": {"wheel_diameter_m": 0.1, "track_width_m": 0.5,
                           "ticks_per_rev": 1000},
                 "samples": [{"t": 0, "right": 0}]}
            )

    def test_unknown_robot_key(self):
        with pytest.raises(ValueError, match="未知"):
            handle_request(
                {"robot": {"wheel_diameter_m": 0.1, "track_width_m": 0.5,
                           "ticks_per_rev": 1000, "wheel_base": 0.5},
                 "samples": [{"t": 0, "left": 0, "right": 0}]}
            )

    def test_initial_pose_respected(self):
        request = load_example("request_straight.json")
        request["initial_pose"] = {"x_m": 1.0, "y_m": 2.0, "theta_rad": 0.0}
        result = handle_request(request)
        assert result["poses"][0]["x_m"] == 1.0
        final = result["summary"]["final_pose"]
        assert final["x_m"] == pytest.approx(1.0 + math.pi, abs=1e-9)
        assert final["y_m"] == pytest.approx(2.0, abs=1e-9)
