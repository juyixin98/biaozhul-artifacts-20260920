"""Service-level tests: status categories, request identity, evidence shape."""
import numpy as np
from fastapi.testclient import TestClient

from krylov_expm import __version__
from krylov_expm.config import SolverConfig
from krylov_expm.service import create_app

from conftest import load_fixture
from reference import reference_expmv, relative_error


def make_client(**cfg_overrides):
    return TestClient(create_app(SolverConfig(**cfg_overrides)))


def payload_from_fixture(name, t, tol=None):
    A, v = load_fixture(name)
    coo = A.tocoo()
    body = {
        "matrix": {
            "shape": [int(x) for x in A.shape],
            "row": coo.row.tolist(),
            "col": coo.col.tolist(),
            "data": coo.data.tolist(),
        },
        "vector": v.tolist(),
        "t": t,
    }
    if tol is not None:
        body["tol"] = tol
    return body


def test_healthz_reports_version():
    client = make_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__}


def test_converged_request_echoes_identity_and_matches_mpmath():
    client = make_client()
    resp = client.post(
        "/v1/expmv",
        json=payload_from_fixture("jordan_block", 2.0, tol=1e-10),
        headers={"X-Request-ID": "test-req-001"},
    )
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == "test-req-001"
    body = resp.json()
    assert body["request_id"] == "test-req-001"
    assert body["status"] == "converged"
    assert body["failure"] is None
    assert body["meta"]["version"] == __version__
    assert "max_krylov_dim" in body["meta"]["config"]
    evidence = body["evidence"]
    assert evidence["planned_segments"] == len(evidence["segments"])
    seg = evidence["segments"][0]
    assert "subspace_residual_norm" in seg
    assert "error_estimate" in seg
    # Independent ground truth: mpmath scaling-and-squaring reference.
    A, v = load_fixture("jordan_block")
    w_ref = reference_expmv(A.toarray(), 2.0, v)
    assert relative_error(np.array(body["vector"]), w_ref) < 1e-7


def test_generated_request_id_when_not_provided():
    client = make_client()
    resp = client.post("/v1/expmv", json=payload_from_fixture("symmetric_tridiag", 0.3))
    body = resp.json()
    assert body["request_id"]
    assert resp.headers["X-Request-ID"] == body["request_id"]


def test_not_converged_is_flagged_not_hidden():
    client = make_client(max_krylov_dim=2, max_restart_splits=1, segment_theta=1.0e9)
    resp = client.post("/v1/expmv", json=payload_from_fixture("grcar_nonnormal", 3.0, tol=1e-14))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "not_converged"
    assert body["vector"] is None
    assert body["failure"]["category"] == "NOT_CONVERGED"
    assert body["failure"]["details"]["error_estimate"] > 1e-14


def test_validation_error_category():
    client = make_client()
    body = payload_from_fixture("symmetric_tridiag", 0.7)
    body["vector"] = [1.0]
    resp = client.post("/v1/expmv", json=body)
    assert resp.status_code == 422
    payload = resp.json()
    assert payload["status"] == "rejected"
    assert payload["failure"]["category"] == "VALIDATION_ERROR"


def test_budget_exceeded_category():
    client = make_client(max_basis_bytes=64)
    resp = client.post("/v1/expmv", json=payload_from_fixture("symmetric_tridiag", 0.7))
    assert resp.status_code == 413
    assert resp.json()["failure"]["category"] == "BUDGET_EXCEEDED"


def test_zero_time_returns_input_vector():
    client = make_client()
    body = payload_from_fixture("grcar_nonnormal", 0.0)
    resp = client.post("/v1/expmv", json=body)
    payload = resp.json()
    assert payload["status"] == "converged"
    assert payload["evidence"]["trivial_case"] == "zero_time"
    assert np.allclose(payload["vector"], body["vector"])


def test_zero_vector_returns_zero_vector():
    client = make_client()
    body = payload_from_fixture("grcar_nonnormal", 1.1)
    body["vector"] = [0.0] * len(body["vector"])
    resp = client.post("/v1/expmv", json=body)
    payload = resp.json()
    assert payload["status"] == "converged"
    assert payload["evidence"]["trivial_case"] == "zero_vector"
    assert np.allclose(payload["vector"], 0.0)


def test_negative_time_via_service_matches_mpmath():
    client = make_client()
    resp = client.post("/v1/expmv", json=payload_from_fixture("symmetric_tridiag", -0.8, tol=1e-10))
    body = resp.json()
    assert body["status"] == "converged"
    A, v = load_fixture("symmetric_tridiag")
    w_ref = reference_expmv(A.toarray(), -0.8, v)
    assert relative_error(np.array(body["vector"]), w_ref) < 1e-7
