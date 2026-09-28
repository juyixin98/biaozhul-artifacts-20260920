"""HTTP API behaviour: correlation ids, structured failures, real results."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from krylov_expv.config import ExpvConfig
from krylov_expv.service.app import create_app

from .conftest import load_fixture


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


def _payload(name: str, **overrides):
    data, _, _ = load_fixture(name)
    body = {
        "matrix": {"n": data["n"], "row": data["row"], "col": data["col"], "data": data["data"]},
        "vector": data["vector"],
        "t": data["t"],
        "tol": data["tol"],
    }
    body.update(overrides)
    return body


def test_health_and_version(client):
    assert client.get("/health").json() == {"status": "ok"}
    version = client.get("/version").json()
    assert version["service"] == "krylov-expv"
    assert set(version["versions"]) == {"krylov_expv", "numpy", "scipy"}


def test_happy_path_returns_result_and_evidence(client):
    response = client.post("/v1/expv", json=_payload("diag3", request_id="req-diag3-1"))
    assert response.status_code == 200
    body = response.json()

    assert body["request_id"] == "req-diag3-1"
    assert body["status"] == "converged"
    assert body["failure"] is None
    # concrete numeric assertion, not just "endpoint responded"
    t = 1.25
    expected = np.array([1.0 * np.exp(-t), -2.0 * np.exp(-0.5 * t), 0.5 * np.exp(0.25 * t)])
    assert np.allclose(body["w"], expected, rtol=1e-10)
    # evidence: steps, residual vs estimate, versions, memory accounting
    assert body["num_steps"] >= 1
    assert body["total_error_estimate"] >= 0.0
    assert body["max_subspace_residual"] >= 0.0
    assert body["memory_bytes_used"] > 0
    assert body["versions"]["krylov_expv"]
    assert body["elapsed_ms"] > 0.0


def test_generated_request_id_when_absent(client):
    response = client.post("/v1/expv", json=_payload("diag3"))
    body = response.json()
    assert body["request_id"]
    assert response.headers["x-request-id"]


def test_invalid_indices_are_rejected_with_category(client):
    body = _payload("diag3")
    body["matrix"]["row"][0] = 99  # out of bounds for n=3
    response = client.post("/v1/expv", json=body)
    assert response.status_code == 400
    payload = response.json()
    assert payload["status"] == "rejected"
    assert payload["failure"]["category"] == "invalid_input"
    assert payload["request_id"]


def test_nan_vector_is_rejected(client):
    import json as jsonlib

    body = _payload("diag3")
    body["vector"][1] = float("nan")
    # httpx's json= refuses non-finite floats; send raw JSON instead
    response = client.post(
        "/v1/expv", content=jsonlib.dumps(body), headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["failure"]["category"] == "invalid_input"


def test_dimension_mismatch_is_rejected(client):
    body = _payload("diag3")
    body["vector"] = [1.0, 2.0]
    response = client.post("/v1/expv", json=body)
    assert response.status_code == 400
    assert response.json()["failure"]["category"] == "invalid_input"


def test_not_converged_is_a_structured_response(client):
    body = _payload("advection60_long", m_max=8, max_steps=2)
    response = client.post("/v1/expv", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "not_converged"
    assert payload["failure"]["category"] == "max_steps_exceeded"
    assert payload["num_steps"] == 2
    assert payload["w"] is not None  # best partial state is still returned


def test_memory_budget_rejection():
    tiny = ExpvConfig(memory_budget_bytes=64)
    client = TestClient(create_app(config=tiny))
    response = client.post("/v1/expv", json=_payload("diag3"))
    assert response.status_code == 413
    assert response.json()["failure"]["category"] == "memory_budget_exceeded"


def test_zero_vector_and_zero_time_via_api(client):
    body = _payload("jordan4")
    body["vector"] = [0.0, 0.0, 0.0, 0.0]
    response = client.post("/v1/expv", json=body)
    assert response.status_code == 200
    assert response.json()["w"] == [0.0, 0.0, 0.0, 0.0]
    assert response.json()["num_steps"] == 0

    body = _payload("jordan4", t=0.0)
    response = client.post("/v1/expv", json=body)
    assert response.status_code == 200
    assert response.json()["w"] == pytest.approx([1.0, 0.5, -0.25, 2.0])
