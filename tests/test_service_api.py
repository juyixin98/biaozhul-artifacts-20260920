"""End-to-end HTTP tests against the real FastAPI app via TestClient."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from config.settings import (
    BudgetConfig,
    LogConfig,
    NumericConfig,
    ServiceConfig,
)
from root_isolator.service.app import create_app


@pytest.fixture
def client():
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        budget=BudgetConfig(),
        numeric=NumericConfig(),
        log=LogConfig(level="DEBUG", redact_polynomials=True),
    )
    with TestClient(create_app(config)) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# Happy paths with concrete asserted results
# --------------------------------------------------------------------------- #
def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_isolate_repeated_root_descending(client):
    # x^3 - 3x + 2 = (x-1)^2 (x+2), descending [1,0,-3,2]
    response = client.post("/api/v1/isolate", json={"coefficients": [1, 0, -3, 2]})
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "accepted"
    counts = body["root_counts"]
    assert counts["distinct_real_roots"] == 2
    assert counts["real_roots_with_multiplicity"] == 3
    intervals = body["intervals"]
    multiplicities = sorted(iv["multiplicity"] for iv in intervals)
    assert multiplicities == [1, 2]
    for interval in intervals:
        # Rationals carry exact numerator/denominator.
        assert {"numerator", "denominator", "decimal_display"} <= set(interval["left"])
        assert interval["proof"]["convention"].startswith("half-open")
        assert interval["independent_evidence"]["descartes"]["agrees"] is True


def test_isolate_sparse_format(client):
    response = client.post(
        "/api/v1/isolate",
        json={"sparse": {"0": -1, "2": 1}},  # x^2 - 1
    )
    assert response.status_code == 200
    body = response.json()
    assert body["root_counts"]["distinct_real_roots"] == 2


def test_exact_fraction_string_accepted(client):
    # x^2 - 1/4 -> roots +/- 1/2
    response = client.post(
        "/api/v1/isolate",
        json={"coefficients": ["1", "0", "-1/4"], "order": "descending"},
    )
    assert response.status_code == 200
    assert response.json()["verdict"] == "accepted"


def test_zero_polynomial_special_response(client):
    response = client.post("/api/v1/isolate", json={"coefficients": [0, 0, 0]})
    assert response.status_code == 200
    body = response.json()
    assert body["result_kind"] == "zero_polynomial"
    assert body["intervals"] == []
    assert "every real" in body["evidence"]["reasons"][0]


def test_no_real_roots(client):
    response = client.post("/api/v1/isolate", json={"coefficients": [1, 0, 1]})
    body = response.json()
    assert response.status_code == 200
    assert body["root_counts"]["distinct_real_roots"] == 0
    assert body["root_counts"]["complex_roots_with_multiplicity"] == 2


def test_response_carries_request_id_and_echoes_header(client):
    response = client.post(
        "/api/v1/isolate",
        json={"coefficients": [1, 0, -1]},
        headers={"X-Request-ID": "req-test-123"},
    )
    assert response.json()["request_id"] == "req-test-123"


def test_response_generates_request_id_when_absent(client):
    response = client.post("/api/v1/isolate", json={"coefficients": [1, 0, -1]})
    assert response.json()["request_id"]


# --------------------------------------------------------------------------- #
# Failure categories with specific status codes and categories
# --------------------------------------------------------------------------- #
def test_binary_float_rejected_with_category(client):
    response = client.post("/api/v1/isolate", json={"coefficients": [1.0, 0, -1]})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["category"] == "NON_EXACT_COEFFICIENT"
    assert "float" in error["message"]


def test_malformed_json_rejected(client):
    response = client.post(
        "/api/v1/isolate",
        content="{not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["category"] == "MALFORMED_COEFFICIENTS"


def test_non_object_body_rejected(client):
    response = client.post("/api/v1/isolate", json=[1, 2, 3])
    assert response.status_code == 422
    assert response.json()["error"]["category"] == "MALFORMED_COEFFICIENTS"


def test_empty_coefficients_rejected(client):
    response = client.post("/api/v1/isolate", json={"coefficients": []})
    assert response.status_code == 422
    assert response.json()["error"]["category"] == "EMPTY_COEFFICIENTS"


def test_degree_budget_rejected_over_http():
    config = ServiceConfig(
        budget=BudgetConfig(max_degree=3),
        numeric=NumericConfig(),
        log=LogConfig(),
    )
    with TestClient(create_app(config)) as small_client:
        response = small_client.post(
            "/api/v1/isolate",
            json={"coefficients": [1, 0, 0, 0, 0]},  # degree 4
        )
    assert response.status_code == 422
    assert response.json()["error"]["category"] == "DEGREE_EXCEEDED"


def test_missing_both_fields_rejected(client):
    response = client.post("/api/v1/isolate", json={"order": "descending"})
    assert response.status_code == 422
    assert response.json()["error"]["category"] == "MALFORMED_COEFFICIENTS"


# --------------------------------------------------------------------------- #
# Diagnostics redaction: raw coefficients never reach the log
# --------------------------------------------------------------------------- #
def test_logs_are_redacted_and_carry_request_id(caplog):
    config = ServiceConfig(
        budget=BudgetConfig(),
        numeric=NumericConfig(),
        log=LogConfig(level="DEBUG", redact_polynomials=True),
    )
    service_logger = logging.getLogger("root_isolator")
    application = create_app(config)
    # create_app/configure_logging resets propagation; enable it AFTER that so
    # pytest's caplog handler can observe records.
    service_logger.propagate = True
    try:
        with TestClient(application) as client:
            secret = 424242424242
            with caplog.at_level(logging.INFO, logger="root_isolator"):
                client.post(
                    "/api/v1/isolate",
                    json={"coefficients": [1, secret, 0, -1], "order": "descending"},
                    headers={"X-Request-ID": "req-redact"},
                )
    finally:
        service_logger.propagate = False

    request_records = [r for r in caplog.records if getattr(r, "request_id", None)]
    assert request_records, "expected at least one request-scoped log record"
    assert all(r.request_id == "req-redact" for r in request_records)
    # The secret coefficient value must never appear; the redaction marker must.
    rendered_states = [
        json.dumps(getattr(r, "state", {}), default=str) for r in request_records
    ]
    assert all(str(secret) not in blob for blob in rendered_states)
    assert any("<redacted>" in blob for blob in rendered_states)
