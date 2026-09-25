"""Tests for the JSON handler and the stdin/stdout CLI."""

import json
import subprocess
import sys

import numpy as np
import pytest

from robustreg.api import handle_request


def _payload():
    rng = np.random.default_rng(123)
    X = rng.normal(size=(40, 2))
    y = 0.5 + X @ np.array([1.0, -2.0]) + rng.normal(scale=0.2, size=40)
    y[:5] += 10.0
    return {"X": X.tolist(), "y": y.tolist()}


def test_huber_success_envelope():
    resp = handle_request(_payload())
    assert resp["ok"] is True
    assert resp["method"] == "huber"
    r = resp["result"]
    assert r["converged"] is True
    assert r["status"] == "converged"
    assert r["objective_decreased"] is True
    assert len(r["coef"]) == 2
    assert r["n"] == 40 and r["p"] == 2
    assert r["rank"] == 3  # intercept + two slopes
    assert r["rank_deficient"] is False
    assert r["nullspace_dim"] == 0
    assert r["iterations"] >= 2
    # JSON round-trip must survive and contain no NaN/Inf.
    text = json.dumps(resp, allow_nan=False)
    again = json.loads(text)
    assert again["result"]["objective"] == r["objective"]


def test_ols_success_envelope_and_objective_comparison():
    h = handle_request(_payload())
    o = handle_request({**_payload(), "method": "ols"})
    assert o["ok"] is True
    assert o["method"] == "ols"
    assert "iterations" not in o["result"]
    assert o["result"]["sse"] > 0
    # Huber endpoint achieves a lower (or equal) Huber objective than OLS.
    assert (h["result"]["objective"]
            <= o["result"]["huber_objective"] + 1e-8)


def test_rank_deficient_error_envelope():
    payload = _payload()
    X = np.array(payload["X"])
    payload["X"] = np.column_stack([X[:, 0], 2.0 * X[:, 0]]).tolist()
    resp = handle_request(payload)
    assert resp["ok"] is False
    err = resp["error"]
    assert err["code"] == "rank_deficient"
    assert err["rank"] == 2
    assert err["p"] == 3
    assert err["nullspace_dim"] == 1
    assert "hint" in err


def test_rank_deficient_can_be_allowed():
    payload = _payload()
    X = np.array(payload["X"])
    payload["X"] = np.column_stack([X[:, 0], X[:, 0]]).tolist()
    payload["allow_rank_deficient"] = True
    resp = handle_request(payload)
    assert resp["ok"] is True
    r = resp["result"]
    assert r["rank_deficient"] is True
    assert r["nullspace_dim"] == 1
    assert r["warnings"]


def test_invalid_json_shape_envelope():
    resp = handle_request({"X": [[1.0]], "y": [1.0, 2.0]})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "invalid_request"


def test_non_dict_body_envelope():
    assert handle_request([1, 2])["ok"] is False
    assert handle_request("nope")["ok"] is False


def test_zero_residual_request_does_not_emit_nan():
    resp = handle_request({
        "X": [[0.0], [1.0], [2.0]],
        "y": [1.0, 3.0, 5.0],
    })
    assert resp["ok"] is True
    r = resp["result"]
    assert all(np.isfinite(r["weights"]))
    assert all(np.isfinite(r["residual"]))
    json.dumps(resp, allow_nan=False)  # raises if NaN/Inf present


def test_max_iter_status_field():
    payload = _payload()
    payload["max_iter"] = 1
    r = handle_request(payload)["result"]
    assert r["iterations"] == 1
    assert r["status"] in ("converged", "max_iter_reached")


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------

def _run_cli(payload):
    proc = subprocess.run(
        [sys.executable, "-m", "robustreg.cli"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_success(tmp_path):
    code, out, err = _run_cli(_payload())
    assert code == 0, err
    resp = json.loads(out)
    assert resp["ok"] is True

    # File input/output path too.
    fin = tmp_path / "req.json"
    fout = tmp_path / "resp.json"
    fin.write_text(json.dumps(_payload()))
    proc = subprocess.run(
        [sys.executable, "-m", "robustreg.cli", "-f", str(fin),
         "-o", str(fout)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(fout.read_text())["ok"] is True


def test_cli_rank_deficient_exit_code():
    payload = _payload()
    X = np.array(payload["X"])
    payload["X"] = np.column_stack([X[:, 0], X[:, 0]]).tolist()
    code, out, _ = _run_cli(payload)
    assert code == 1
    assert json.loads(out)["error"]["code"] == "rank_deficient"


def test_cli_invalid_json_exit_code():
    proc = subprocess.run(
        [sys.executable, "-m", "robustreg.cli"],
        input="{not json",
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["error"]["code"] == "invalid_json"


@pytest.mark.parametrize("override", [
    {"delta": -1},
    {"method": "bad"},
    {"y": [1.0]},
    {"bogus_field": 1},
])
def test_cli_invalid_request_exit_code(override):
    payload = _payload()
    payload.update(override)
    code, out, _ = _run_cli(payload)
    assert code == 1
    assert json.loads(out)["error"]["code"] == "invalid_request"
