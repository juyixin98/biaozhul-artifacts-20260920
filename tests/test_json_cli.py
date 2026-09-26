"""Tests for the JSON request/response layer and the CLI entry point."""

import json
import subprocess
import sys

import numpy as np
import pytest

from icp2d import run_request
from icp2d.synthetic import rectangle_room_scan, straight_wall_scan, make_scan_pair


def _room_request():
    world = rectangle_room_scan(spacing=0.05)
    scenario = make_scan_pair(
        world, np.array([0.8, -0.5, 0.15]),
        noise_sigma=0.01, outlier_count=180, overlap_ratio=0.7, seed=2,
    )
    return {
        "source": scenario.source.tolist(),
        "target": scenario.target.tolist(),
        "initial_guess": [0.0, 0.0, 0.0],
    }


def test_json_request_full_pipeline_converges():
    response = run_request(_room_request())
    assert response["success"]
    assert response["status"] == "converged"
    assert response["converged"]
    assert not response["uncertain"]
    assert abs(response["pose"]["x"] - 0.8) < 0.01
    assert abs(response["pose"]["y"] + 0.5) < 0.01
    assert abs(response["pose"]["theta"] - 0.15) < np.deg2rad(1.0)
    assert len(response["residual_history"]) == response["iterations"]
    assert response["degeneracy"]["degenerate"] is False


def test_json_response_marks_wall_as_uncertain():
    wall = straight_wall_scan(length=20.0, spacing=0.05)
    scenario = make_scan_pair(wall, np.array([1.0, 0.3, 0.0]),
                              noise_sigma=0.005, seed=3)
    response = run_request({
        "source": scenario.source.tolist(),
        "target": scenario.target.tolist(),
    })
    assert response["success"]  # an estimate exists ...
    assert response["uncertain"]  # ... but it is flagged uncertain
    assert response["degeneracy"]["degenerate"] is True
    direction = response["degeneracy"]["flat_translation_direction"]
    assert abs(direction[0]) > 0.9


def test_json_config_override_is_honoured():
    request = _room_request()
    request["config"] = {"max_iterations": 3}
    response = run_request(request)
    assert response["iterations"] == 3
    assert response["status"] == "max_iterations_reached"


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        {"source": [[0, 0], [1, 0]]},  # too few points / no target
        {"source": [[0, 0], [1, 0], [0, 1]], "target": "x"},
        {"source": [[0, 0], [1, 0], [0, 1]],
         "target": [[0, 0], [1, 0], [0, 1]], "initial_guess": [0, 0]},
        {"source": [[0, 0], [1, 0], [0, 1]],
         "target": [[0, 0], [1, 0], [0, 1]], "config": {"nope": 1}},
    ],
)
def test_invalid_requests_raise_value_error(payload):
    with pytest.raises(ValueError):
        run_request(payload)


def test_cli_file_input_outputs_valid_json(tmp_path):
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_room_request()))
    proc = subprocess.run(
        [sys.executable, "-m", "icp2d", str(request_path)],
        capture_output=True, text=True, cwd=__import__("pathlib").Path(__file__).parents[1],
    )
    assert proc.returncode == 0, proc.stderr
    response = json.loads(proc.stdout)
    assert response["success"]
    assert response["status"] == "converged"


def test_cli_exit_code_one_for_insufficient_inliers(tmp_path):
    world = rectangle_room_scan(spacing=0.05)
    scenario = make_scan_pair(world, np.array([0.8, -0.5, 0.15]), seed=1)
    request = {
        "source": scenario.source.tolist(),
        "target": scenario.target.tolist(),
        "initial_guess": [10.0, 10.0, 0.0],
    }
    request_path = tmp_path / "bad_request.json"
    request_path.write_text(json.dumps(request))
    proc = subprocess.run(
        [sys.executable, "-m", "icp2d", str(request_path)],
        capture_output=True, text=True, cwd=__import__("pathlib").Path(__file__).parents[1],
    )
    assert proc.returncode == 1
    response = json.loads(proc.stdout)
    assert not response["success"]
    assert response["status"] == "insufficient_inliers"


def test_cli_exit_code_two_for_malformed_json(tmp_path):
    request_path = tmp_path / "broken.json"
    request_path.write_text("{not valid")
    proc = subprocess.run(
        [sys.executable, "-m", "icp2d", str(request_path)],
        capture_output=True, text=True, cwd=__import__("pathlib").Path(__file__).parents[1],
    )
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["success"] is False


def test_cli_stdin_input():
    proc = subprocess.run(
        [sys.executable, "-m", "icp2d", "-"],
        input=json.dumps(_room_request()), capture_output=True, text=True,
        cwd=__import__("pathlib").Path(__file__).parents[1],
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["success"]
