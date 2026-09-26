"""JSON 入口测试:请求校验与端到端结果。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from occupancy_grid.io import run_request

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def minimal_request():
    return {
        "grid": {"width": 10, "height": 10, "resolution": 1.0},
        "sensor_model": {"p_occ": 0.7, "p_free": 0.4},
        "scans": [
            {
                "pose": {"x": 0.5, "y": 0.5, "theta": 0.0},
                "angles": [0.0],
                "ranges": [3.0],
                "max_range": 8.0,
            }
        ],
    }


def test_run_request_shapes_and_stats():
    result = run_request(minimal_request())
    assert result["grid"]["width"] == 10
    assert len(result["log_odds"]) == 10
    assert len(result["log_odds"][0]) == 10
    stats = result["scan_stats"][0]
    assert stats["ray_count"] == 1
    assert stats["hit_count"] == 1
    # 手算:终点单元 (0,3) 占据
    assert result["log_odds"][0][3] == pytest.approx(0.8472978603872037)
    # 概率与 log-odds 一致
    assert result["probability"][0][3] == pytest.approx(0.7)


def test_invalid_grid_rejected():
    req = minimal_request()
    req["grid"]["resolution"] = 0.0
    with pytest.raises(ValueError):
        run_request(req)


def test_cli_end_to_end(tmp_path):
    req_path = tmp_path / "request.json"
    out_path = tmp_path / "result.json"
    req_path.write_text(json.dumps(minimal_request()), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "main.py"),
         "--request", str(req_path), "--output", str(out_path)],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert result["scan_stats"][0]["hit_count"] == 1
    assert result["probability"][0][3] == pytest.approx(0.7)


def test_example_request_file_runs(tmp_path):
    """随仓库提供的样例请求必须能直接跑通。"""
    out_path = tmp_path / "example_result.json"
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "main.py"),
         "--request", str(PROJECT_ROOT / "examples" / "request.json"),
         "--output", str(out_path)],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(result["scan_stats"]) == 2
