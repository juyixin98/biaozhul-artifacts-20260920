"""Integration tests through the full FastAPI stack.

Covers HTTP semantics for accept / partial / not-met / singular / invalid
categories, request-id correlation, and payload redaction in logs.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Config
from app.logging_setup import configure_logging
from app.numerical import mp, workprec
from tests.fixtures import conditioned_matrix, exact_singular_matrix

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    configure_logging("DEBUG")
    app = create_app(Config.load())
    with TestClient(app) as test_client:
        yield test_client


def _mp_to_payload(a, b, extra_config=None):
    with workprec(80):
        payload = {
            "A": [[mp.nstr(a[i, j], 40) for j in range(a.cols)] for i in range(a.rows)],
            "B": [
                [mp.nstr(b[i, j], 40) for j in range(b.cols)] for i in range(b.rows)
            ],
        }
    if extra_config is not None:
        payload["config"] = extra_config
    return payload


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_well_conditioned_solve_accepted(client):
    payload = {
        "A": [[4, 1, 0], [1, 3, 1], [0, 1, 2]],
        "B": [3, 5, 4],
    }
    resp = client.post("/solve", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["columns"][0]["status"] == "accepted"
    # Known exact solution is (0.5, 1.0, 1.5).
    x = [float(row[0]) for row in body["solution"]]
    assert x == pytest.approx([0.5, 1.0, 1.5], abs=1e-9)


def test_request_id_header_is_echoed_and_correlated(client, caplog):
    payload = {"A": [[2, 0], [0, 4]], "B": [2, 8]}
    with caplog.at_level(logging.INFO, logger="mipsolver"):
        resp = client.post(
            "/solve", json=payload, headers={"X-Request-ID": "trace-abc-123"}
        )
    assert resp.status_code == 200
    assert resp.json()["request_id"] == "trace-abc-123"
    correlated = [r for r in caplog.records if getattr(r, "request_id", None) == "trace-abc-123"]
    assert correlated, "no log record carried the request correlation id"


def test_singular_system_returns_422_with_evidence(client):
    a, b = exact_singular_matrix()
    resp = client.post("/solve", json=_mp_to_payload(a, b))
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "singular_inconclusive"
    assert body["solution"] is None
    assert "rank 2/3" in body["reason"]
    assert all(c["status"] == "skipped_numerically_singular" for c in body["columns"])


def test_unreachable_tolerance_returns_422_not_200(client):
    a, b, _, _ = conditioned_matrix(6, 18, seed=3)
    payload = _mp_to_payload(
        a, b, {"backward_tol": "1e-150", "mp_dps_ladder": [20, 40]}
    )
    resp = client.post("/solve", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "not_met"
    assert body["columns"][0]["status"] == "not_met"
    assert "No convergence reported" in body["columns"][0]["reason"]


def test_malformed_payload_returns_4xx(client):
    # Missing 'B': Pydantic rejects at the boundary with a 422 validation error.
    resp = client.post("/solve", json={"A": [[1, 0], [0, 1]]})
    assert resp.status_code in (400, 422)
    assert "B" in resp.text


def test_non_square_matrix_returns_400_invalid_request(client):
    resp = client.post(
        "/solve", json={"A": [[1, 0, 0], [0, 1, 0]], "B": [1, 2]}
    )
    assert resp.status_code == 400
    assert resp.json()["status"] == "invalid_request"
    assert "square" in resp.json()["reason"]


def test_unknown_config_key_rejected(client):
    payload = {
        "A": [[1, 0], [0, 1]],
        "B": [1, 1],
        "config": {"residual_dps": 90},  # not overridable per request
    }
    resp = client.post("/solve", json=payload)
    assert resp.status_code == 400
    assert resp.json()["status"] == "invalid_request"


def test_sensitive_entries_never_logged(client, caplog):
    # A distinctive "secret" coefficient that must never appear verbatim.
    secret = "0.12345678901234567890123456789SECRET"
    # Build a valid numeric analogue with a rare mantissa to hunt in logs.
    rare = "0.917263540817263540817263540817"
    payload = {"A": [[rare, "0"], ["0", "1"]], "B": [rare, "2"]}
    with caplog.at_level(logging.DEBUG, logger="mipsolver"):
        resp = client.post("/solve", json=payload)
    assert resp.status_code == 200
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert rare not in log_text
    assert secret not in log_text  # guard: the probe value itself absent


def test_batch_partial_acceptance_reports_columns_independently(client):
    # Two RHS aligned to largest/smallest singular vectors; one-iteration fp32
    # budget drives exactly one column over the line (mirrors the unit test,
    # but through HTTP).
    from tests.fixtures import directional_rhs_system

    a, b2, _ = directional_rhs_system(10, 8, seed=42)
    payload = _mp_to_payload(
        a,
        b2,
        {
            "use_fp32_first": True,
            "use_fp64": False,
            "mp_dps_ladder": [],
            "backward_tol": "5e-9",
            "max_iterations_per_stage": 1,
        },
    )
    resp = client.post("/solve", json=payload)
    assert resp.status_code in (200, 422)
    body = resp.json()
    assert body["status"] == "partially_accepted"
    assert len(body["columns"]) == 2
    assert {c["column"] for c in body["columns"]} == {0, 1}
    assert body["columns"][1]["status"] == "accepted"
    assert body["columns"][0]["status"] == "not_met"
