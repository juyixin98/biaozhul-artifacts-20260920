import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def run_cli(*args: str, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "trajvel.cli", *args],
        capture_output=True,
        text=True,
        input=stdin_text,
        cwd=ROOT,
        check=False,
    )


def test_cli_line_example_from_file():
    proc = run_cli(str(EXAMPLES / "request_line.json"))
    assert proc.returncode == 0, proc.stderr
    response = json.loads(proc.stdout)
    assert response["total_time"] == pytest.approx(7.0, abs=1e-9)
    assert response["node_velocities"][0] == 0.0
    assert response["node_velocities"][-1] == 0.0
    assert max(response["samples"]["speeds"]) <= 2.0 + 1e-6
    assert max(abs(a) for a in response["samples"]["accelerations"]) <= 1.0 + 1e-6


def test_cli_sharp_corner_example():
    proc = run_cli(str(EXAMPLES / "request_sharp_corner.json"))
    assert proc.returncode == 0, proc.stderr
    response = json.loads(proc.stdout)
    assert response["corner_velocities"]["1"] < 2.0
    assert response["node_velocities"][1] == pytest.approx(
        response["corner_velocities"]["1"]
    )
    assert response["total_time"] > 0.0


def test_cli_zero_length_example_no_nan():
    proc = run_cli(str(EXAMPLES / "request_zero_length_stop.json"))
    assert proc.returncode == 0, proc.stderr
    response = json.loads(proc.stdout)
    assert response["meta"]["removed_duplicate_points"] == 1
    assert response["node_velocities"][1] == 0.0  # stop strategy at corner
    assert all(v == v for v in response["node_velocities"])  # no NaN
    assert response["total_time"] > 0.0


def test_cli_stdin_to_stdout():
    request = {
        "waypoints": [[0.0, 0.0], [1.0, 0.0]],
        "constraints": {
            "max_velocity": 1.0,
            "max_acceleration": 1.0,
            "max_lateral_acceleration": 1.0,
        },
    }
    proc = run_cli(stdin_text=json.dumps(request))
    assert proc.returncode == 0, proc.stderr
    response = json.loads(proc.stdout)
    # 1 m, triangular profile with amax=1: peak 1 m/s, total 2 s.
    assert response["total_time"] == pytest.approx(2.0, abs=1e-9)
    assert "samples" not in response  # no sample_dt requested


def test_cli_output_file(tmp_path):
    out = tmp_path / "response.json"
    proc = run_cli(str(EXAMPLES / "request_line.json"), str(out))
    assert proc.returncode == 0, proc.stderr
    response = json.loads(out.read_text())
    assert response["total_time"] == pytest.approx(7.0, abs=1e-9)


def test_cli_invalid_request_reports_json_error():
    proc = run_cli(stdin_text='{"waypoints": [[0, 0]]}')
    assert proc.returncode == 2
    error = json.loads(proc.stderr)
    assert "error" in error


def test_cli_bad_corner_strategy_rejected():
    request = {
        "waypoints": [[0.0, 0.0], [1.0, 0.0]],
        "constraints": {
            "max_velocity": 1.0,
            "max_acceleration": 1.0,
            "max_lateral_acceleration": 1.0,
        },
        "corner_strategy": "teleport",
    }
    proc = run_cli(stdin_text=json.dumps(request))
    assert proc.returncode == 2
    assert "corner_strategy" in json.loads(proc.stderr)["error"]
