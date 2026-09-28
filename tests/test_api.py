"""End-to-end tests through the FastAPI layer + service orchestration.

These assert concrete numeric results, distinct error categories/status
codes, state-version conflicts and run-log replay content - not merely that
an endpoint responds.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from hvpsvc.main import create_app

from . import fixtures as fx


@pytest.fixture
def client(service):
    return TestClient(create_app(service), raise_server_exceptions=True)


def _create(client, spec, **extra):
    payload = {"variables": spec["variables"],
               "expression": spec["expression"]}
    payload.update(extra)
    r = client.post("/functions", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["state_id"]


# --------------------------------------------------------------------------
# Happy path with concrete values
# --------------------------------------------------------------------------

def test_hvp_endpoint_quadratic_concrete_values(client):
    sid = _create(client, fx.quadratic3_spec())
    r = client.post(f"/functions/{sid}/point",
                    json={"point": {"x": fx.X_QUAD3.tolist()}})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1

    g = client.get(f"/functions/{sid}/gradient").json()
    np.testing.assert_allclose(
        g["gradient"]["x"], fx.H_QUAD3 @ fx.X_QUAD3, rtol=1e-11, atol=1e-11)

    r = client.post(f"/functions/{sid}/hvp",
                    json={"vector": {"x": fx.V_QUAD3.tolist()}})
    assert r.status_code == 200, r.text
    body = r.json()
    np.testing.assert_allclose(
        body["hvp"]["x"], fx.H_QUAD3 @ fx.V_QUAD3, rtol=1e-11, atol=1e-11)
    assert body["version"] == 1
    assert body["run_id"].startswith("run-")


def test_zero_vector_reports_exact_zero(client):
    sid = _create(client, fx.nonlinear_spec())
    client.post(f"/functions/{sid}/point",
                json={"point": {"x": fx.X_NONLINEAR.tolist()}})
    r = client.post(f"/functions/{sid}/hvp",
                    json={"vector": {"x": [0.0, 0.0, 0.0]}})
    assert r.status_code == 200
    body = r.json()
    assert body["zero_direction"] is True
    assert all(v == 0.0 for v in body["hvp"]["x"])


def test_scalar_and_matrix_variable_layouts(client):
    sid = _create(client, fx.two_variable_spec())
    client.post(f"/functions/{sid}/point",
                json={"point": {"x": [1.5], "y": -0.8}})
    # missing variable in vector -> 422 input_error
    r = client.post(f"/functions/{sid}/hvp", json={"vector": {"x": [1.0]}})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "input_error"
    # wrong inner shape -> 422
    r2 = client.post(f"/functions/{sid}/hvp",
                     json={"vector": {"x": [1.0, 2.0], "y": 0.0}})
    assert r2.status_code == 422
    assert r2.json()["error"]["category"] == "input_error"
    # correct binding works
    r3 = client.post(f"/functions/{sid}/hvp",
                     json={"vector": {"x": [-1.0], "y": 2.0}})
    assert r3.status_code == 200
    hv = r3.json()["hvp"]
    np.testing.assert_allclose(hv["x"], [-4.0], atol=1e-11)
    assert hv["y"] == pytest.approx(5.0, abs=1e-11)


# --------------------------------------------------------------------------
# Verify endpoint with independent reference
# --------------------------------------------------------------------------

def test_verify_endpoint_passes_all_checks(client):
    sid = _create(client, fx.nonlinear_spec())
    client.post(f"/functions/{sid}/point",
                json={"point": {"x": fx.X_NONLINEAR.tolist()}})
    r = client.post(f"/functions/{sid}/verify",
                    json={"vector": {"x": [1.0, -2.0, 0.5]},
                          "tolerance": 1e-8})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["passed"] is True, body["checks"]
    names = {c["name"] for c in body["checks"]}
    assert "gradient_vs_mpmath" in names
    assert "gradient_vs_central_differences" in names
    assert "hvp_zero_direction" in names
    assert "hessian_symmetry_basis_probe" in names
    # explicit reference Hessian is exposed for cross-checking
    H = np.array(body["ref_hessian"])
    assert H.shape == (3, 3)
    assert np.allclose(H, H.T)
    failed = [c for c in body["checks"] if not c["passed"]]
    assert failed == []


def test_verify_records_concrete_expected_and_actual(client):
    sid = _create(client, fx.quadratic3_spec())
    client.post(f"/functions/{sid}/point",
                json={"point": {"x": fx.X_QUAD3.tolist()}})
    body = client.post(f"/functions/{sid}/verify", json={}).json()
    hvp_check = next(c for c in body["checks"]
                     if c["name"] == "hvp_vs_hessian[random_seed_0x4A50]")
    assert hvp_check["passed"] is True
    assert len(hvp_check["expected"]) == 3
    assert hvp_check["max_rel_error"] < 1e-8


# --------------------------------------------------------------------------
# Error categories must be distinguishable end to end
# --------------------------------------------------------------------------

def test_unknown_state_is_state_conflict(client):
    r = client.get("/functions/nope/gradient")
    assert r.status_code == 409
    assert r.json()["error"]["category"] == "state_conflict"


def test_operation_before_point_is_state_conflict(client):
    sid = _create(client, fx.quadratic3_spec())
    r = client.post(f"/functions/{sid}/hvp",
                    json={"vector": {"x": [1.0, 0.0, 0.0]}})
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["category"] == "state_conflict"
    assert "set-point" in body["message"]


def test_stale_version_conflict(client):
    sid = _create(client, fx.quadratic3_spec())
    r1 = client.post(f"/functions/{sid}/point",
                     json={"point": {"x": [0.0, 0.0, 0.0]},
                           "expected_version": 0})
    assert r1.status_code == 200
    r2 = client.post(f"/functions/{sid}/point",
                     json={"point": {"x": [1.0, 0.0, 0.0]},
                           "expected_version": 0})
    assert r2.status_code == 409
    assert r2.json()["error"]["detail"]["current_version"] == 1


def test_nonsmooth_rejected_with_category(client):
    sid = _create(client, fx.nonsmooth_spec())
    client.post(f"/functions/{sid}/point", json={"point": {"z": [0.0, 1.0]}})
    r = client.get(f"/functions/{sid}/gradient")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["category"] == "nonsmooth_point"
    assert err["detail"]["op"] == "abs"


def test_subgradient_policy_allows_kink_and_verify_skips_smooth_checks(client):
    sid = _create(client, fx.nonsmooth_spec(),
                  nonsmooth={"policy": "subgradient", "subgradient": 0.25})
    client.post(f"/functions/{sid}/point", json={"point": {"z": [0.0, 0.0]}})
    g = client.get(f"/functions/{sid}/gradient")
    assert g.status_code == 200
    np.testing.assert_allclose(
        g.json()["gradient"]["z"], [-0.25, 1.0], atol=1e-12)
    body = client.post(f"/functions/{sid}/verify", json={}).json()
    skip = next(c for c in body["checks"] if c.get("skipped"))
    assert "kink" in skip["reason"]
    zero = next(c for c in body["checks"] if c["name"] == "hvp_zero_direction")
    assert zero["passed"] is True


def test_compute_failure_domain_and_overflow(client):
    sid = _create(client, fx.log_domain_spec())
    r = client.post(f"/functions/{sid}/point", json={"point": {"x": -2.0}})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "compute_failure"

    sid2 = _create(client, fx.exp_spec())
    r2 = client.post(f"/functions/{sid2}/point", json={"point": {"x": 1000.0}})
    assert r2.status_code == 422
    assert r2.json()["error"]["category"] == "compute_failure"


def test_resource_exhaustion_node_budget(client):
    r = client.post("/functions", json={
        "variables": fx.quadratic3_spec()["variables"],
        "expression": fx.quadratic3_spec()["expression"],
        "budget": {"max_nodes": 5, "max_evals": 1_000_000}})
    assert r.status_code == 429
    assert r.json()["error"]["category"] == "resource_exhausted"
    assert r.json()["error"]["detail"]["limit"] == "max_nodes"


def test_malformed_expression_is_input_error(client):
    r = client.post("/functions", json={
        "variables": [{"name": "x", "shape": []}],
        "expression": {"nodes": [{"id": "n", "op": "bogus",
                                  "args": ["x"]}], "output": "n"}})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["category"] == "input_error"
    assert "known_ops" in err["detail"]


# --------------------------------------------------------------------------
# Run log replay
# --------------------------------------------------------------------------

def test_run_log_replays_intermediates_and_judgement(client, service):
    sid = _create(client, fx.quadratic3_spec())
    client.post(f"/functions/{sid}/point",
                json={"point": {"x": fx.X_QUAD3.tolist()}})
    hvp_body = client.post(
        f"/functions/{sid}/hvp",
        json={"vector": {"x": fx.V_QUAD3.tolist()}}).json()
    run_id = hvp_body["run_id"]

    entry = service.logger.get(run_id)
    assert entry is not None
    assert entry["operation"] == "hvp"
    assert entry["status"] == "ok"
    assert entry["state_id"] == sid
    # replay-critical intermediate state is present
    assert "tangent_nodes_visited" in entry["intermediates"]
    assert entry["intermediates"]["v_norm"] == pytest.approx(
        float(np.linalg.norm(fx.V_QUAD3)))
    assert entry["result_summary"]["hvp_norm"] == pytest.approx(
        float(np.linalg.norm(fx.H_QUAD3 @ fx.V_QUAD3)))

    # the same run is retrievable through the API
    via_api = client.get(f"/runs/{run_id}")
    assert via_api.status_code == 200
    assert via_api.json()["run_id"] == run_id


def test_failed_run_is_logged_with_category(client, service):
    sid = _create(client, fx.nonsmooth_spec())
    client.post(f"/functions/{sid}/point", json={"point": {"z": [0.0, 5.0]}})
    r = client.get(f"/functions/{sid}/gradient")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["category"] == "nonsmooth_point"
    assert err["detail"]["op"] == "abs"
    run_id = err["run_id"]
    assert run_id and run_id.startswith("run-")

    # the failure can be replayed by id with its category and intermediate info
    replay = client.get(f"/runs/{run_id}")
    assert replay.status_code == 200
    entry = replay.json()
    assert entry["operation"] == "gradient"
    assert entry["status"] == "error"
    assert entry["error"]["category"] == "nonsmooth_point"


def test_healthz_lists_ops(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert {"add", "mul", "exp", "abs", "max"} <= set(body["ops"])
