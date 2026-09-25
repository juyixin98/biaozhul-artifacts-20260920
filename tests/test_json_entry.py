"""JSON 入口与几何工具的单元测试。"""

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from main import run_request
from occupancy_grid import Pose2D, bresenham, sensor_to_world, world_to_grid

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


# ---- 几何工具 -----------------------------------------------------------


def test_sensor_to_world_rotation_and_translation():
    pose = Pose2D(x=1.0, y=2.0, theta=math.pi / 2)
    wx, wy = sensor_to_world(pose, 1.0, 0.0)
    assert wx == pytest.approx(1.0)
    assert wy == pytest.approx(3.0)


def test_world_to_grid_floors_to_cell():
    assert world_to_grid(0.5, 0.5, 0.0, 0.0, 1.0) == (0, 0)
    assert world_to_grid(3.5, 2.5, 0.0, 0.0, 1.0) == (3, 2)
    assert world_to_grid(-0.1, 0.0, 0.0, 0.0, 1.0) == (-1, 0)
    # 带原点偏移
    assert world_to_grid(1.6, 1.6, 1.0, 1.0, 0.5) == (1, 1)


def test_bresenham_straight_and_diagonal():
    assert bresenham(0, 0, 3, 0) == [(0, 0), (1, 0), (2, 0), (3, 0)]
    assert bresenham(0, 0, 2, 2) == [(0, 0), (1, 1), (2, 2)]
    cells = bresenham(0, 0, 3, 1)
    assert cells[0] == (0, 0) and cells[-1] == (3, 1)
    assert len(cells) == 4  # 每步 x 前进一格


# ---- JSON 入口 ----------------------------------------------------------


def test_run_request_single_ray_example():
    request = json.loads((EXAMPLES / "request_single_ray.json").read_text())
    result = run_request(request)

    assert result["grid"]["width"] == 10
    assert result["scan_stats"] == [{"hit": 1, "miss": 0}]
    log_odds = result["log_odds"]
    assert log_odds[0][0] == pytest.approx(-0.4)
    assert log_odds[0][3] == pytest.approx(0.85)
    assert log_odds[0][4] == 0.0  # 障碍后方不受影响
    assert result["probabilities"][0][3] == pytest.approx(0.7, abs=1e-3)


def test_run_request_repeated_example_accumulates():
    request = json.loads((EXAMPLES / "request_repeated.json").read_text())
    result = run_request(request)
    assert result["log_odds"][0][3] == pytest.approx(2 * 0.85)
    assert result["log_odds"][0][0] == pytest.approx(2 * -0.4)


def test_run_request_out_of_bounds_example():
    request = json.loads((EXAMPLES / "request_out_of_bounds.json").read_text())
    result = run_request(request)
    assert result["scan_stats"] == [{"hit": 0, "miss": 4}]
    # 四条未命中射线只产生空闲更新
    assert all(v <= 0.0 for row in result["log_odds"] for v in row)


def test_run_request_validation_error():
    with pytest.raises(ValueError, match="scans"):
        run_request({"grid": {"width": 4, "height": 4, "resolution": 1.0}, "scans": []})


def test_cli_end_to_end(tmp_path):
    out = tmp_path / "result.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "main.py"),
            "--input",
            str(EXAMPLES / "request_single_ray.json"),
            "--output",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(out.read_text())
    assert result["log_odds"][0][3] == pytest.approx(0.85)


def test_cli_reports_error_on_bad_input(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"grid": {}}')
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--input", str(bad)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "error" in proc.stderr
