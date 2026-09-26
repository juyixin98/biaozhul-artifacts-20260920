"""Tests for the JSON command-line entry point."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT_ROOT / "examples"


def _run_cli(*args: str, stdin: str | None = None) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "scan_matching.cli", *args],
        capture_output=True, text=True, cwd=PROJECT_ROOT, input=stdin)
    assert proc.stdout, f"no stdout; stderr={proc.stderr}"
    return proc.returncode, json.loads(proc.stdout)


def test_scenario_request_succeeds():
    code, response = _run_cli(str(EXAMPLES / "request_scenario_room.json"))
    assert code == 0
    assert response["success"]
    result = response["result"]
    assert result["converged"]
    assert not result["degenerate"]
    assert result["pose_error"]["translation"] < 0.05
    assert len(result["iterations"]) >= 2
    # Ground truth is reported for scenario requests.
    truth = result["ground_truth"]["pose"]
    assert abs(result["pose"]["x"] - truth["x"]) < 0.05


def test_degenerate_scenario_request_reports_uncertainty():
    code, response = _run_cli(
        str(EXAMPLES / "request_scenario_line_degenerate.json"))
    assert code == 0
    assert response["success"]
    result = response["result"]
    assert result["degenerate"]
    assert result["degenerate_direction"] is not None
    assert "uncertain" in result["message"]


def test_explicit_clouds_via_stdin():
    request = (EXAMPLES / "request_explicit.json").read_text()
    code, response = _run_cli("-", stdin=request)
    assert code == 0
    assert response["success"]
    pose = response["result"]["pose"]
    # The target is the source shifted by (+0.5, +0.2).
    assert pose["x"] == pytest.approx(0.5, abs=0.05)
    assert pose["y"] == pytest.approx(0.2, abs=0.05)


def test_invalid_request_returns_error_envelope():
    code, response = _run_cli("-", stdin='{"foo": 1}')
    assert code == 1
    assert not response["success"]
    assert "source" in response["error"]["message"]


def test_malformed_json_returns_error_envelope():
    code, response = _run_cli("-", stdin="not json {")
    assert code == 1
    assert not response["success"]


def test_missing_file_returns_error_envelope():
    code, response = _run_cli("does_not_exist.json")
    assert code == 1
    assert not response["success"]
    assert response["error"]["type"] == "FileNotFoundError"


def test_failed_match_has_null_residual_not_infinity():
    """A failed run must still emit strict-valid JSON (no Infinity)."""
    request = {
        "source": [[100.0, 100.0], [101.0, 100.0], [100.0, 101.0]],
        "target": [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
        "params": {"max_correspondence_distance": 1.0},
    }
    code, response = _run_cli("-", stdin=json.dumps(request))
    assert code == 0
    result = response["result"]
    assert result["status"] == "failed"
    assert result["residual"] is None
