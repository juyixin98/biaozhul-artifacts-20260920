"""End-to-end HTTP integration tests via FastAPI's in-process TestClient.

These assert concrete results and the distinct failure categories, not mere
"endpoint is reachable" checks.
"""

from __future__ import annotations

import json
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from rational_linalg.service.app import create_app

from .conftest import fixture_matrix, fixture_vector, load_fixture, payload_fraction


@pytest.fixture
def client(log_dir):
    return TestClient(create_app())


def _post(client, path, body):
    response = client.post(path, json=body)
    return response, response.json()


def test_health_and_error_catalog(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert "exact" in response.json()["arithmetic"]
    catalog = client.get("/api/v1/error-codes").json()["categories"]
    assert set(catalog) == {
        "INPUT_ERROR",
        "STATE_CONFLICT",
        "RESOURCE_EXHAUSTED",
        "COMPUTATION_FAILED",
    }


def test_solve_unique_exact_payload_and_evidence(client):
    data = load_fixture("big_common_factor.json")
    response, body = _post(
        client,
        "/api/v1/solve",
        {
            "A": data["A"],
            "b": data["b"],
            "include_float_diagnosis": True,
        },
    )
    assert response.status_code == 200, body
    assert body["classification"] == "unique"
    assert body["rank"] == {"A": 2, "augmented": 2}
    x = [payload_fraction(v) for v in body["solution"]["particular"]]
    assert x == [Fraction(1), Fraction(2)]
    assert body["solution"]["evidence_ok"] is True
    assert all(v == "0/1" for v in body["solution"]["residual_Ap_minus_b"])
    assert payload_fraction(body["determinant"]) == -Fraction(10**40)
    # The lossy comparison is explicitly labelled and present on request.
    assert body["float_diagnosis"]["float_dtype"] == "float64"
    assert body["arithmetic"].startswith("exact rational")
    assert body["log_event_count"] >= 2


def test_solve_infinite_returns_parametric_form(client):
    data = load_fixture("rank_deficient_infinite.json")
    response, body = _post(client, "/api/v1/solve", data)
    assert response.status_code == 200
    assert body["classification"] == "infinite"
    assert body["solution"]["free_columns"] == [2]
    null_vec = [
        payload_fraction(v)
        for v in body["solution"]["null_space_basis"][0]
    ]
    assert null_vec == [Fraction(1), Fraction(-2), Fraction(1)]
    # Re-substitute the parametric point through the HTTP-delivered fractions.
    A = fixture_matrix(data["A"])
    b = fixture_vector(data["b"])
    particular = [
        payload_fraction(v) for v in body["solution"]["particular"]
    ]
    t = Fraction(5, 2)
    x = [p + t * d for p, d in zip(particular, null_vec)]
    residual = [
        sum(a * xi for a, xi in zip(row, x)) - rhs
        for row, rhs in zip(A, b)
    ]
    assert residual == [Fraction(0), Fraction(0), Fraction(0)]


def test_solve_inconsistent_returns_contradiction_witness(client):
    data = load_fixture("inconsistent_pair.json")
    response, body = _post(client, "/api/v1/solve", data)
    assert response.status_code == 200
    assert body["classification"] == "inconsistent"
    contradiction = body["contradiction"]
    assert contradiction["annihilates_A"] is True
    assert contradiction["contradicts_b"] is True
    assert contradiction["evidence_ok"] is True
    assert abs(payload_fraction(contradiction["yT_b"])) == Fraction(1)
    y = [payload_fraction(v) for v in contradiction["witness_y"]]
    # Independently re-derive the certificate from the original system.
    for j in range(2):
        assert sum(y[i] * data["A"][i][j] for i in range(2)) == 0
    assert sum(y[i] * data["b"][i] for i in range(2)) != 0


def test_rank_endpoint_exact_rank_and_left_nullspace(client):
    data = load_fixture("near_float_indistinguishable.json")
    response, body = _post(
        client,
        "/api/v1/rank",
        {"A": data["A"], "include_float_diagnosis": True},
    )
    assert response.status_code == 200
    assert body["rank"] == 2
    assert body["float_diagnosis"]["rank_mismatch_vs_exact"] is True
    assert body["float_diagnosis"]["numpy_rank_default_tol"] == 1
    assert body["evidence_ok"] is True
    assert payload_fraction(body["determinant"]) == Fraction(-1)


def test_float_number_in_json_is_input_error_not_silent_conversion(client):
    response = client.post(
        "/api/v1/solve", json={"A": [[0.1, 0.2], [0.3, 0.4]], "b": [1, 1]}
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["category"] == "INPUT_ERROR"
    assert error["code"] == "NON_EXACT_NUMBER"


def test_ragged_matrix_is_input_error(client):
    response = client.post(
        "/api/v1/solve", json={"A": [[1, 2], [3]], "b": [1, 1]}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "RAGGED_MATRIX"


def test_dimension_mismatch_is_distinguishable_input_error(client):
    response = client.post(
        "/api/v1/solve", json={"A": [[1, 2], [3, 4]], "b": [1]}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DIMENSION_MISMATCH"


def test_schema_validation_error_is_input_error(client):
    response = client.post("/api/v1/solve", json={"A": [[1]]})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SCHEMA_VALIDATION_FAILED"


def test_resource_exhausted_returns_507_with_progress(client):
    n = 6
    H = [[f"1/{i + j + 1}" for j in range(n)] for i in range(n)]
    response = client.post(
        "/api/v1/solve", json={"A": H, "b": ["1"] * n, "digit_budget": 5}
    )
    assert response.status_code == 507
    error = response.json()["error"]
    assert error["category"] == "RESOURCE_EXHAUSTED"
    assert error["code"] == "DIGIT_BUDGET_EXCEEDED"
    progress = error["progress"]
    assert 1 <= progress["completed_pivots"] < n
    assert progress["budget"]["digit_limit"] == 5
    assert progress["partial_matrix"]
    assert progress["digit_trace"]


def test_unknown_run_id_is_state_conflict(client):
    response = client.get("/api/v1/runs/does-not-exist")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_run_replay_endpoint_reports_full_trace(client):
    response = client.post(
        "/api/v1/solve",
        json={
            "A": [[1, 1], [1, -1]],
            "b": [3, 1],
            "run_id": "fixed-run-id-1",
        },
    )
    assert response.status_code == 200
    replay = client.get("/api/v1/runs/fixed-run-id-1").json()
    assert replay["status"] == "completed"
    event_names = {e["event"] for e in replay["events"]}
    assert {"request_parsed", "pivot_selected", "elimination_done"} <= event_names
    assert replay["result"]["classification"] == "unique"


def test_duplicate_run_id_is_state_conflict(client):
    body = {"A": [[1, 0], [0, 1]], "b": [1, 1], "run_id": "dup"}
    first = client.post("/api/v1/solve", json=body)
    assert first.status_code == 200
    second = client.post("/api/v1/solve", json=body)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "RUN_ID_CONFLICT"


def test_rank_budget_exhaustion_is_507_with_progress(client):
    n = 6
    H = [[f"1/{i + j + 1}" for j in range(n)] for i in range(n)]
    response = client.post("/api/v1/rank", json={"A": H, "digit_budget": 4})
    assert response.status_code == 507
    error = response.json()["error"]
    assert error["code"] == "DIGIT_BUDGET_EXCEEDED"
    assert error["progress"]["partial_matrix"]


def test_app_run_entrypoint_is_importable(client):
    # Smoke test the console-script callable without binding a socket.
    from rational_linalg.service import app as app_module

    assert callable(app_module.run)
    assert app_module.app.title.startswith("Exact Rational")


def test_rectangular_overdetermined_and_inconsistent_over_http(client):
    data = load_fixture("overdetermined_consistent.json")
    response, body = _post(client, "/api/v1/solve", data)
    assert response.status_code == 200
    x = [payload_fraction(v) for v in body["solution"]["particular"]]
    assert x == [Fraction(2), Fraction(-1)]

    # Flip one RHS entry to make the overdetermined system inconsistent.
    broken = dict(data)
    broken["b"] = [3, 3, 7]
    response, body = _post(client, "/api/v1/solve", broken)
    assert response.status_code == 200
    assert body["classification"] == "inconsistent"
    assert body["contradiction"]["evidence_ok"] is True
