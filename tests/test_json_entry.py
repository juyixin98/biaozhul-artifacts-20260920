"""JSON 入口与合成传感器测试。"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from trajectory_planning.json_entry import run_request
from trajectory_planning.synthetic import compare_odometry, simulate_odometry
from trajectory_planning.planner import build_profile

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_run_request_straight():
    req = json.loads((REPO_ROOT / "examples/request_straight.json").read_text())
    result = run_request(req)
    assert result["status"] == "ok"
    assert result["validation"]["passed"]
    np.testing.assert_allclose(result["summary"]["total_time"], 5.0, rtol=1e-6)
    assert "samples" in result


def test_run_request_zero_length_example():
    req = json.loads((REPO_ROOT / "examples/request_zero_length.json").read_text())
    result = run_request(req)
    assert result["status"] == "ok"
    assert result["summary"]["removed_duplicate_nodes"] == 1
    assert result["validation"]["passed"]


def test_run_request_infeasible_returns_error():
    req = json.loads((REPO_ROOT / "examples/request_infeasible.json").read_text())
    result = run_request(req)
    assert result["status"] == "error"
    assert result["error_type"] == "InfeasibleTrajectory"
    assert "起点速度" in result["message"]


def test_run_request_curvature_example():
    req = json.loads(
        (REPO_ROOT / "examples/request_corner_curvature.json").read_text()
    )
    result = run_request(req)
    assert result["status"] == "ok"
    assert result["validation"]["passed"]
    v_corner = result["node_speeds"][1]
    np.testing.assert_allclose(v_corner, np.sqrt(0.3), rtol=1e-6)


def test_synthetic_odometry_error_stats():
    profile, _ = build_profile(
        [[0, 0], [2, 0], [2, 2]],
        {"v_max": 1.0, "a_max": 0.5, "d_max": 0.5},
        {"strategy": "stop"},
    )
    odo = simulate_odometry(profile, dt=0.02, seed=42)
    stats = compare_odometry(odo)
    # 噪声标准差 1mm / 0.01m/s，RMSE 应在同一量级
    assert stats["position_error_rmse"] < 0.01
    assert stats["speed_error_rmse"] < 0.05
    assert stats["n_samples"] > 100


def test_cli_file_input(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "trajectory_planning.json_entry",
         str(REPO_ROOT / "examples/request_straight.json")],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "ok"


def test_cli_stdin_and_error_exit_code():
    bad = json.dumps({
        "points": [[0, 0], [0.1, 0]],
        "dynamics": {"v_max": 5, "a_max": 0.5, "d_max": 0.5,
                     "v_start": 3, "v_end": 0},
    })
    proc = subprocess.run(
        [sys.executable, "-m", "trajectory_planning.json_entry"],
        cwd=REPO_ROOT, input=bad, capture_output=True, text=True,
    )
    assert proc.returncode == 1
    result = json.loads(proc.stdout)
    assert result["status"] == "error"


def test_cli_missing_file_exit_code():
    proc = subprocess.run(
        [sys.executable, "-m", "trajectory_planning.json_entry",
         "/nonexistent/request.json"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 2
