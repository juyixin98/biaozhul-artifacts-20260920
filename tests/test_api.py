"""Tests for the JSON entry point (api.solve_request and the CLI)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ccd.api import solve_request

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def head_on_request():
    return {
        "time_window": [0.0, 10.0],
        "robot": {"position": [0.0, 0.0], "velocity": [1.0, 0.0], "radius": 1.0},
        "obstacles": [
            {"id": "obs-1", "position": [10.0, 0.0], "velocity": [-1.0, 0.0], "radius": 1.0}
        ],
    }


def test_solve_request_head_on():
    response = solve_request(head_on_request())
    assert response["earliest"]["obstacle_id"] == "obs-1"
    assert response["earliest"]["status"] == "collision"
    assert response["earliest"]["time"] == pytest.approx(4.0, abs=1e-12)
    assert response["results"][0]["distance_at_contact"] == pytest.approx(2.0)


def test_solve_request_picks_earliest_obstacle():
    request = head_on_request()
    request["obstacles"].append(
        {"id": "closer", "position": [6.0, 0.0], "velocity": [-1.0, 0.0], "radius": 1.0}
    )
    response = solve_request(request)
    assert response["earliest"]["obstacle_id"] == "closer"
    assert response["earliest"]["time"] == pytest.approx(2.0, abs=1e-12)
    assert len(response["results"]) == 2


def test_solve_request_no_time_window_defaults_to_infinite():
    request = head_on_request()
    del request["time_window"]
    response = solve_request(request)
    assert response["earliest"]["time"] == pytest.approx(4.0, abs=1e-12)


def test_solve_request_no_collision():
    request = head_on_request()
    request["time_window"] = [0.0, 3.0]
    response = solve_request(request)
    assert response["earliest"] is None
    assert response["results"][0]["status"] == "no_collision"


def test_invalid_requests_raise_value_error():
    with pytest.raises(ValueError):
        solve_request({})
    with pytest.raises(ValueError):
        solve_request({"robot": {"position": [0, 0], "velocity": [0, 0], "radius": 1}, "obstacles": []})
    with pytest.raises(ValueError):
        solve_request({"robot": {"position": [0, 0], "velocity": [0, 0], "radius": -1}, "obstacles": [{}]})
    with pytest.raises(ValueError):
        solve_request({"robot": {"position": [0, 0, 0], "velocity": [0, 0], "radius": 1}, "obstacles": [{}]})


def test_cli_roundtrip(tmp_path):
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(head_on_request()))
    proc = subprocess.run(
        [sys.executable, "-m", "ccd", str(request_file)],
        capture_output=True, text=True, check=False, cwd=PROJECT_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    response = json.loads(proc.stdout)
    assert response["earliest"]["time"] == pytest.approx(4.0, abs=1e-12)


def test_cli_invalid_input_exits_2(tmp_path):
    request_file = tmp_path / "bad.json"
    request_file.write_text('{"robot": {}}')
    proc = subprocess.run(
        [sys.executable, "-m", "ccd", str(request_file)],
        capture_output=True, text=True, check=False, cwd=PROJECT_ROOT,
    )
    assert proc.returncode == 2
    assert "error" in json.loads(proc.stderr)
