"""End-to-end HTTP integration tests via FastAPI's in-process ASGI client."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from rationalsvc import api


@pytest.fixture()
def client(tmp_run_log):
    return TestClient(api.app)


def test_health_reports_exact_backend(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "fractions.Fraction" in body["exact_backend"]
    assert "mpmath" in body["approximate_diagnosis"]


def test_solve_unique_system_exact_json(client, case):
    r = client.post("/solve", json=case("big_common_factor"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["exact"] is True
    assert body["run_id"].startswith("run-")
    assert body["solutions"][0]["particular"] == ["2", "1"]
    assert body["verification"]["all_residuals_zero"] is True


def test_infinite_and_inconsistent_classifications(client, case):
    r = client.post("/solve", json=case("rank_deficient_infinite"))
    assert r.json()["solutions"][0]["classification"] == "infinite"

    r = client.post("/solve", json=case("rank_deficient_inconsistent"))
    sol = r.json()["solutions"][0]
    assert sol["classification"] == "inconsistent"
    assert sol["contradiction_witness"]["y_dot_b"] == "-2"


def test_near_float_case_exact_solution_and_float_diagnosis(client, case):
    r = client.post("/solve", json=case("near_float_indistinguishable"))
    body = r.json()
    assert body["solutions"][0]["particular"] == ["1", "1", "1"]
    diag = body["float_diagnosis"]
    assert diag["rank_float64"] == 2
    assert diag["rank_exact"] == 3
    assert diag["double_confuses_exact_structure"] is True


def test_float_input_returns_specific_error_class(client, input_error_case):
    r = client.post("/solve", json=input_error_case("float_refused")["payload"])
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "input_precision_unsupported"
    assert body["run_id"]


def test_shape_mismatch_error_class(client, input_error_case):
    r = client.post("/solve", json=input_error_case("ragged")["payload"])
    assert r.status_code == 400
    assert r.json()["error"] == "input_shape_mismatch"


def test_empty_input_error_class(client, input_error_case):
    r = client.post("/solve", json=input_error_case("empty_matrix")["payload"])
    assert r.status_code == 400
    assert r.json()["error"] == "input_empty"


def test_missing_field_error_class(client, input_error_case):
    r = client.post("/solve", json=input_error_case("missing_A")["payload"])
    assert r.json()["error"] == "input_malformed"


def test_state_conflict_error_class_and_status(client, input_error_case):
    r = client.post("/solve", json=input_error_case("bad_budget")["payload"])
    assert r.status_code == 409
    assert r.json()["error"] == "state_conflict"


def test_invalid_json_body_is_input_malformed(client):
    r = client.post("/solve", content="{not json", headers={
        "content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"] == "input_malformed"


def test_budget_exhausted_http_status_and_progress(client, case):
    r = client.post("/solve", json=case("budget_too_tight"))
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "budget_exhausted"
    p = body["details"]["progress"]
    assert "matrix" in p and "pivots" in p and "swaps" in p
    assert body["run_id"]


def test_rank_endpoint_returns_rank_block_only(client, case):
    r = client.post("/rank", json=case("near_float_indistinguishable"))
    assert r.status_code == 200
    body = r.json()
    assert body["rank"]["rank_A"] == 3
    assert body["rank"]["nullity"] == 0
    # rank-only request omits solutions / verification / float diagnosis
    assert "solutions" not in body
    assert "verification" not in body
    assert "float_diagnosis" not in body


def test_want_rank_in_body_also_omits_solutions(client, case):
    import copy
    payload = copy.deepcopy(case("rank_deficient_infinite"))
    payload["want"] = "rank"
    body = client.post("/solve", json=payload).json()
    assert body["rank"]["rank_A"] == 2
    assert "solutions" not in body


def test_run_replay_endpoint_returns_all_events(client, case):
    solved = client.post("/solve", json=case("big_common_factor")).json()
    rid = solved["run_id"]
    r = client.get(f"/runs/{rid}")
    assert r.status_code == 200
    events = r.json()["events"]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "started"
    assert "checkpoint" in kinds
    assert kinds[-1] == "completed"


def test_run_replay_unknown_id_is_404(client):
    r = client.get("/runs/run-doesnotexist")
    assert r.status_code == 404
    assert r.json()["error"] == "input_empty"


def test_failed_run_is_also_logged_and_replayable(client, case):
    failed = client.post("/solve", json=case("budget_too_tight")).json()
    rid = failed["run_id"]
    r = client.get(f"/runs/{rid}")
    assert r.status_code == 200
    kinds = [e["event"] for e in r.json()["events"]]
    assert "budget_exhausted" in kinds


def test_unexpected_internal_failure_is_computation_failed_500(
    client, case, monkeypatch
):
    def boom(*a, **k):
        raise RuntimeError("simulated kernel defect")

    monkeypatch.setattr(api.runner, "eliminate", boom)
    r = client.post("/solve", json=case("big_common_factor"))
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "computation_failed"
    assert body["details"]["exception_type"] == "RuntimeError"
    assert body["run_id"]
