"""End-to-end HTTP tests through FastAPI (TestClient)."""
from __future__ import annotations

import numpy as np
import pytest

from fastapi.testclient import TestClient

from app.main import app
from app.numerical_input.fixtures import (
    banded, grid_laplacian, indefinite_3x3, negative_diagonal)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _matrix_body(fx):
    return {"n": fx.n, "rows": [int(x) for x in fx.rows],
            "cols": [int(x) for x in fx.cols],
            "values": [float(x) for x in fx.vals]}


def test_health_reports_versions(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "numpy" in body["versions"]
    assert body["versions"]["numpy"] != "unknown"


def test_solve_endpoint_matches_lapack(client):
    fx = grid_laplacian(5, 4)
    resp = client.post("/api/v1/solve", json={
        "matrix": _matrix_body(fx),
        "rhs": [float(v) for v in fx.rhs],
        "ordering": "minimum_degree"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert len(body["run_id"]) >= 8
    x = np.array(body["solution"])
    # dense LAPACK reference built straight from COO
    a = np.zeros((fx.n, fx.n))
    for r, c, v in zip(fx.rows, fx.cols, fx.vals):
        a[r, c] = v
    x_ref = np.linalg.solve(a, fx.rhs)
    np.testing.assert_allclose(x, x_ref, rtol=1e-8, atol=1e-10)
    assert body["evidence"]["passed"] is True
    assert body["report"]["nnz_l"] > 0


def test_solve_nonspd_returns_classified_error_not_500(client):
    fx = indefinite_3x3()
    resp = client.post("/api/v1/solve", json={
        "matrix": _matrix_body(fx),
        "rhs": [float(v) for v in fx.rhs],
        "ordering": "natural"})
    body = resp.json()
    assert body["status"] == "error"
    assert body["error_code"] == "NON_SPD_PIVOT"
    assert body["error_category"] == "numerical"
    assert body["details"]["original_index"] == 1
    assert "run_id" in body


def test_solve_asymmetric_returns_input_error(client):
    resp = client.post("/api/v1/solve", json={
        "matrix": {"n": 2, "rows": [0, 0, 1, 1],
                   "cols": [0, 1, 0, 1],
                   "values": [2.0, -1.0, -0.9, 2.0]},
        "rhs": [1.0, 1.0]})
    body = resp.json()
    assert body["status"] == "error"
    assert body["error_code"] == "ASYMMETRIC"
    assert body["error_category"] == "input"


def test_factor_endpoint_diagonal_positive(client):
    fx = banded(10)
    resp = client.post("/api/v1/factor", json={
        "matrix": _matrix_body(fx), "ordering": "natural"})
    body = resp.json()
    assert body["status"] == "ok"
    assert all(d > 0 for d in body["diag_d"])
    assert len(body["diag_d"]) == fx.n


def test_ordering_compare_endpoint(client):
    fx = grid_laplacian(7, 6)
    resp = client.post("/api/v1/orderings/compare",
                       json={"matrix": _matrix_body(fx)})
    body = resp.json()
    assert body["status"] == "ok"
    methods = {r["method"]: r for r in body["orderings"]}
    assert set(methods) == {"natural", "minimum_degree",
                            "nested_dissection"}


def test_fixture_endpoint_expected_failure(client):
    resp = client.post("/api/v1/fixtures/run",
                       json={"name": "negative_diagonal"})
    body = resp.json()
    assert body["status"] == "error"
    assert body["expected_failure"] is True
    assert body["error_code"] == "NON_SPD_PIVOT"
    assert body["expected_pivot"] == 3


def test_fixture_endpoint_success(client):
    resp = client.post("/api/v1/fixtures/run",
                       json={"name": "grid", "ordering": "minimum_degree"})
    body = resp.json()
    assert body["status"] == "ok"
    assert body["evidence"]["passed"] is True


def test_unknown_fixture_is_error(client):
    resp = client.post("/api/v1/fixtures/run", json={"name": "nope"})
    assert resp.status_code == 404


def test_malformed_json_is_422(client):
    resp = client.post("/api/v1/solve", json={"matrix": {"n": -1}})
    assert resp.status_code == 422
