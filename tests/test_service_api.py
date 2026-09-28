"""Service-layer and HTTP API tests, including run-correlated logging."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from config.settings import FactorizationConfig
from sparse_cholesky.api import create_app
from sparse_cholesky.api.logging_setup import RunLogger
from sparse_cholesky.api.service import (
    ServiceFailure,
    ServiceSuccess,
    analyze_and_solve,
)
from sparse_cholesky.core import FactorizationEngine
from sparse_cholesky.input.fixtures import (
    grid2d_laplacian,
    non_positive_definite_fixture,
)
from sparse_cholesky.input.matrix import build_sparse_matrix


def _coo_payload(fx):
    return {
        "n": fx.n,
        "entries": [
            {"row": int(r), "col": int(c), "value": float(v)}
            for r, c, v in zip(fx.rows, fx.cols, fx.vals)
        ],
    }


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


def test_service_success_reports_evidence_and_fill():
    fx = grid2d_laplacian(4)
    matrix = build_sparse_matrix(*fx.keys())
    x_true = np.ones(matrix.n)
    b = matrix.csc @ x_true
    logger = RunLogger("", enabled=False)
    outcome = analyze_and_solve(
        matrix, ordering="rcm", rhs=b, run_id="run-success-0000",
        logger=logger,
    )
    assert isinstance(outcome, ServiceSuccess)
    assert outcome.run_id == "run-success-0000"
    assert outcome.fill["fill_in"] > 0
    assert outcome.fill["bandwidth_after"] <= outcome.fill["bandwidth_before"]
    assert outcome.pivots["min"] > 0.0
    rel = outcome.evidence["residual"]["relative_residual_inf"]
    assert rel < 1e-11
    assert outcome.evidence["reconstruction"]["factor_pattern_mismatches"] == 0
    assert outcome.evidence["reconstruction"]["max_cancellation"] < 1e-10
    assert outcome.solution is not None


def test_service_non_pd_is_failure_not_success():
    fx = non_positive_definite_fixture("negative_diag", n=8)
    matrix = build_sparse_matrix(*fx.keys())
    outcome = analyze_and_solve(matrix, run_id="run-fail-0000")
    assert isinstance(outcome, ServiceFailure)
    assert outcome.success is False
    assert outcome.error_type == "non_positive_definite_error"
    assert outcome.pivot_index is not None
    assert outcome.pivot_value is not None
    assert outcome.pivot_value <= 0.0


def test_service_log_records_are_correlated_by_run_id(tmp_path):
    fx = grid2d_laplacian(3)
    matrix = build_sparse_matrix(*fx.keys())
    b = np.ones(matrix.n)
    logger = RunLogger(str(tmp_path))
    analyze_and_solve(matrix, rhs=b, run_id="run-20260928T000000-abc12345",
                      logger=logger)
    log_files = list(Path(tmp_path).glob("*.jsonl"))
    assert len(log_files) == 1
    records = [json.loads(line) for line in log_files[0].read_text().splitlines()]
    # Every record belongs to the same run and carries versions at start.
    assert all(r["run_id"] == "run-20260928T000000-abc12345" for r in records)
    started = [r for r in records if r["step"] == "run" and r["status"] == "started"]
    assert started and "numpy" in started[0]["payload"]["versions"]
    # Progress through the pipeline must be visible.
    steps = {r["step"] for r in records}
    assert {"symbolic:done", "numeric:done", "evidence:residual"} <= steps
    assert any(r["status"] == "succeeded" for r in records)


def test_service_failure_log_records_category_not_success(tmp_path):
    fx = non_positive_definite_fixture("zero_pivot", n=5)
    matrix = build_sparse_matrix(*fx.keys())
    logger = RunLogger(str(tmp_path))
    outcome = analyze_and_solve(matrix, run_id="run-20260928T000000-deadbeef",
                                logger=logger)
    assert isinstance(outcome, ServiceFailure)
    log_files = list(Path(tmp_path).glob("*.jsonl"))
    records = [json.loads(line) for line in log_files[0].read_text().splitlines()]
    failed = [r for r in records if r["status"] == "failed"]
    assert failed
    assert failed[-1]["payload"]["error_type"] == "non_positive_definite_error"
    assert not any(r["status"] == "succeeded" for r in records)


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    app = create_app(log_dir=str(tmp_path))
    return TestClient(app)


def test_http_health_reports_versions(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "numpy" in body["versions"]


def test_http_solve_success(client):
    fx = grid2d_laplacian(3)
    matrix = build_sparse_matrix(*fx.keys())
    b = (matrix.csc @ np.ones(matrix.n)).tolist()
    resp = client.post("/api/v1/solve", json={
        "matrix": _coo_payload(fx),
        "rhs": b,
        "ordering": "rcm",
        "run_id": "run-http-0001",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["run_id"] == "run-http-0001"
    assert len(body["solution"]) == fx.n
    assert body["evidence"]["residual"]["relative_residual_inf"] < 1e-11
    assert body["fill"]["nnz_l"] >= body["fill"]["nnz_a_lower"]


def test_http_symbolic_reports_fill(client):
    fx = grid2d_laplacian(4)
    resp = client.post("/api/v1/symbolic", json={"matrix": _coo_payload(fx)})
    assert resp.status_code == 200
    fill = resp.json()["fill"]
    assert fill["fill_in"] == fill["nnz_l"] - fill["nnz_a_lower"]
    assert fill["ordering"] == "natural"


def test_http_non_pd_returns_categorized_failure(client):
    fx = non_positive_definite_fixture("indefinite", n=6)
    resp = client.post("/api/v1/factorize", json={"matrix": _coo_payload(fx)})
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error_type"] == "non_positive_definite_error"
    assert body["pivot_index"] is not None
    assert body["pivot_value"] is not None
    assert body["run_id"]


def test_http_invalid_shape_is_not_success(client):
    bad = {"n": 2, "entries": [{"row": 5, "col": 0, "value": 1.0},
                               {"row": 0, "col": 0, "value": 1.0},
                               {"row": 1, "col": 1, "value": 1.0}]}
    resp = client.post("/api/v1/factorize", json={"matrix": bad})
    assert resp.status_code == 422
    assert resp.json()["success"] is False


def test_http_rhs_length_mismatch_is_categorized(client):
    fx = grid2d_laplacian(3)
    resp = client.post("/api/v1/solve", json={
        "matrix": _coo_payload(fx),
        "rhs": [1.0, 2.0],  # n=9
    })
    assert resp.status_code == 422
    assert resp.json()["error_type"] == "matrix_shape_error"


def test_http_above_diagonal_entry_rejected(client):
    bad = {"n": 2, "entries": [
        {"row": 0, "col": 0, "value": 2.0},
        {"row": 1, "col": 1, "value": 2.0},
        {"row": 0, "col": 1, "value": -1.0},  # above diagonal
    ]}
    resp = client.post("/api/v1/factorize", json={"matrix": bad})
    assert resp.status_code == 422  # pydantic validator or input check
