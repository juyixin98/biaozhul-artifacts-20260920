"""HTTP interface tests (in-process ASGI via httpx)."""

import numpy as np
import pytest

httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sym_eig.api.app import create_app  # noqa: E402
from sym_eig.config import Settings  # noqa: E402


@pytest.fixture
def client():
    settings = Settings(max_n=64, reference_max_n=24, reference_dps=40)
    return TestClient(create_app(settings))


def test_health_reports_version_algorithm_and_config(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service_version"]
    assert "householder" in body["algorithm"]
    assert body["config"]["max_n"] == 64


def test_success_endpoint_returns_full_evidence(client):
    matrix = np.array([[4.0, 1.0], [1.0, 2.0]])
    resp = client.post(
        "/api/v1/eigendecomposition",
        json={"matrix": matrix.tolist(), "request_id": "api-case-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "SUCCESS"
    assert body["request_id"] == "api-case-1"
    expected = np.linalg.eigvalsh(matrix)
    np.testing.assert_allclose(body["eigenvalues"], expected, atol=1e-12)
    assert body["reference_comparison"]["source"] == "mpmath.eigh"
    assert all(g["passed"] for g in body["gates"])
    assert body["trace"][0]["step"] == "validate_input"
    assert body["processing_location"]["service_version"]


def test_request_id_header_is_honored(client):
    resp = client.post(
        "/api/v1/eigendecomposition",
        json={"matrix": [[1.0, 0.0], [0.0, 2.0]]},
        headers={"X-Request-ID": "header-correlation-7"},
    )
    assert resp.json()["request_id"] == "header-correlation-7"


def test_generated_request_id_when_absent(client):
    resp = client.post(
        "/api/v1/eigendecomposition",
        json={"matrix": [[1.0, 0.0], [0.0, 2.0]]},
    )
    assert len(resp.json()["request_id"]) > 0


def test_non_symmetric_returns_classified_422(client):
    resp = client.post(
        "/api/v1/eigendecomposition",
        json={"matrix": [[1.0, 5.0], [0.0, 1.0]]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["verdict"] == "FAILED"
    assert body["error_category"] == "NON_SYMMETRIC"
    assert body["error_details"]["skew_inf_norm"] == pytest.approx(5.0)


def test_non_convergence_returns_classified_422(client):
    n = 30
    off = np.ones(n - 1)
    laplacian = 2.0 * np.eye(n) - np.diag(off, 1) - np.diag(off, -1)
    resp = client.post(
        "/api/v1/eigendecomposition",
        json={"matrix": laplacian.tolist(), "max_iters": 1,
              "reference": "none"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_category"] == "NON_CONVERGENCE"
    assert body["error_details"]["sweeps_used"] == 1


def test_invalid_payload_shape_422(client):
    resp = client.post(
        "/api/v1/eigendecomposition", json={"matrix": [[1.0], [1.0, 2.0]]}
    )
    assert resp.status_code == 422
    assert resp.json()["error_category"] == "INVALID_MATRIX"


def test_all_malformed_matrices_use_classified_envelope(client):
    # None of these may produce FastAPI's default raw validation error; every
    # one must be the classified FAILED/INVALID_MATRIX envelope with an id.
    payloads = [
        {"matrix": [["x", 0.0], [0.0, 1.0]]},   # non-numeric entry
        {"matrix": [[True, 0.0], [0.0, 1.0]]},  # bool must NOT be coerced
        {"matrix": "abc"},                       # wrong top-level type
        {"not_matrix": 1},                       # missing field
    ]
    for body in payloads:
        resp = client.post("/api/v1/eigendecomposition", json=body)
        assert resp.status_code == 422, body
        out = resp.json()
        assert out["verdict"] == "FAILED"
        assert out["error_category"] == "INVALID_MATRIX"
        assert bool(out["request_id"])


def test_uncertain_is_http_200_with_explicit_uncertainties(client):
    # Correct answer but an impossible gate -> 200 with verdict UNCERTAIN.
    rng = np.random.default_rng(0)
    m = rng.standard_normal((5, 5))
    a = m + m.T
    resp = client.post(
        "/api/v1/eigendecomposition",
        json={
            "matrix": a.tolist(),
            "residual_rtol": 1e-18,
            "orthogonality_tol": 1e-18,
            "reconstruction_rtol": 1e-18,
            "reference": "none",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "UNCERTAIN"
    assert body["eigenvalues"] is not None
    assert len(body["uncertainties"]) >= 1


def test_repeated_spectrum_over_http(client):
    rng = np.random.default_rng(3)
    q = np.linalg.qr(rng.standard_normal((6, 6)))[0]
    lam = np.array([1.0, 1.0, 2.0, 2.0, 3.0, 3.0])
    a = (q * lam) @ q.T
    resp = client.post(
        "/api/v1/eigendecomposition", json={"matrix": a.tolist()}
    )
    body = resp.json()
    assert resp.status_code == 200, body["error_message"]
    assert [c["multiplicity"] for c in body["clusters"]] == [2, 2, 2]
    assert body["reference_comparison"][
        "degenerate_clusters_compared_as_subspaces"
    ] == 3
