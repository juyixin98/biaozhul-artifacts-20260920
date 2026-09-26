"""End-to-end tests for the JSON CLI and the synthetic data generator."""

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from edf import brute_force_field
from edf.synthetic import simulate_mission

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "request_sample.json"


def run_cli(*args, stdin_text=None):
    return subprocess.run(
        [sys.executable, "-m", "edf.cli", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        input=stdin_text,
    )


def test_cli_sample_request_matches_brute_force():
    assert SAMPLE.exists(), "examples/request_sample.json is missing"
    proc = run_cli(str(SAMPLE))
    assert proc.returncode == 0, proc.stderr
    resp = json.loads(proc.stdout)
    req = json.loads(SAMPLE.read_text())

    occ = [[False] * req["width"] for _ in range(req["height"])]
    for x, y in req["obstacles"]:
        occ[y][x] = True
    dist_ref, src_ref = brute_force_field(occ, tuple(req["cell_size"]))

    assert resp["algorithm"] == "felzenszwalb-huttenlocher"
    for y in range(req["height"]):
        for x in range(req["width"]):
            got = resp["distances"][y][x]
            ref = dist_ref[y, x]
            if math.isinf(ref):
                assert got is None
            else:
                assert got == pytest.approx(ref)
            expected_src = src_ref[y][x]
            got_src = resp["sources"][y][x]
            assert got_src == (list(expected_src) if expected_src else None)


def test_cli_stdin_stdout_all_empty():
    req = {"width": 3, "height": 2, "obstacles": []}
    proc = run_cli("-", stdin_text=json.dumps(req))
    assert proc.returncode == 0, proc.stderr
    resp = json.loads(proc.stdout)
    assert resp["distances"] == [[None] * 3, [None] * 3]
    assert resp["sources"] == [[None] * 3, [None] * 3]


def test_cli_occupancy_form_and_output_file(tmp_path):
    req = {
        "width": 2,
        "height": 2,
        "cell_size": [0.5, 0.5],
        "occupancy": [[1, 0], [0, 0]],
    }
    req_path = tmp_path / "req.json"
    out_path = tmp_path / "resp.json"
    req_path.write_text(json.dumps(req))
    proc = run_cli(str(req_path), "-o", str(out_path))
    assert proc.returncode == 0, proc.stderr
    resp = json.loads(out_path.read_text())
    assert resp["distances"][0][0] == 0.0
    assert resp["distances"][1][1] == pytest.approx(math.sqrt(0.5))
    assert resp["sources"][1][1] == [0, 0]


@pytest.mark.parametrize(
    "req",
    [
        {"width": 0, "height": 2, "obstacles": []},  # non-positive width
        {"width": 2, "height": 2, "cell_size": [0, 1], "obstacles": []},
        {"width": 2, "height": 2, "obstacles": [[2, 0]]},  # out of bounds
        {"width": 2, "height": 2, "obstacles": [[0, 0]], "occupancy": [[0, 0], [0, 0]]},
        {"width": 2, "height": 2},  # neither obstacles nor occupancy
        {"width": 2, "height": 2, "occupancy": [[0, 0]]},  # wrong row count
    ],
)
def test_cli_invalid_requests_exit_2(req):
    proc = run_cli("-", stdin_text=json.dumps(req))
    assert proc.returncode == 2
    assert "error" in proc.stderr


def test_cli_missing_file_exit_2():
    proc = run_cli("/nonexistent/request.json")
    assert proc.returncode == 2
    assert "error" in proc.stderr


def test_synthetic_mission_deterministic_and_in_bounds():
    m1 = simulate_mission(seed=7)
    m2 = simulate_mission(seed=7)
    assert m1 == m2
    req = m1["request"]
    assert req["obstacles"], "synthetic mission should perceive some obstacles"
    for x, y in req["obstacles"]:
        assert 0 <= x < req["width"]
        assert 0 <= y < req["height"]
    assert len(m1["trajectory"]) == len(m1["scans"])


def test_synthetic_obstacles_are_real_world_obstacles():
    # The lidar only reports hits on occupied cells, so every perceived
    # obstacle cell must be occupied in the generating world.
    from edf.synthetic import synthetic_world

    seed = 7
    mission = simulate_mission(seed=seed)
    world = synthetic_world(seed, 24, 18)
    for x, y in mission["request"]["obstacles"]:
        assert world[y][x]
