"""End-to-end HTTP tests through FastAPI's TestClient.

These assert concrete payload values, error categories, request-id
propagation and redaction - not merely that the endpoint responds.
"""
from __future__ import annotations

import math


def test_health_and_info(client):
    health = client.get("/health").json()
    assert health["ok"] is True
    assert "request_id" in health and len(health["request_id"]) == 16

    info = client.get("/api/v1/info").json()
    assert set(info["methods"]["monolithic"]) == {"naive", "kahan", "pairwise"}
    assert "kahan_streaming" in info["methods"]["chunked"]
    assert "naive_sharded" in info["methods"]["chunked"]
    assert "big_cancel" in info["scenarios"]


def test_compare_big_cancel_payload(client):
    resp = client.post(
        "/api/v1/compare",
        json={"values": [1e16] + [1] * 10 + [-1e16], "block_size": 4},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["n"] == 12
    assert body["reference"]["value"] == 10.0
    assert body["reference"]["method"] == "mpmath"
    original = body["orderings"]["original"]["methods"]
    assert original["naive"]["result"] == 0.0
    assert original["kahan"]["result"] == 10.0
    assert original["naive"]["verdict"] == "accepted"
    invariants = body["orderings"]["original"]["invariants"]
    assert invariants["blocked_naive_equals_naive"] is True
    assert invariants["kahan_streaming_equals_monolithic"] is True
    assert invariants["kahan_merged_meets_compensated_bound"] is True
    # Correlation id is echoed in body and header.
    assert resp.headers["x-request-id"] == body["request_id"]


def test_client_supplied_request_id_is_honoured(client):
    resp = client.post(
        "/api/v1/compare",
        headers={"x-request-id": "fixed-correlation-id"},
        json={"values": [0.1] * 10},
    )
    assert resp.headers["x-request-id"] == "fixed-correlation-id"
    assert resp.json()["request_id"] == "fixed-correlation-id"


def test_special_tokens_parse_and_policy_fires(client):
    resp = client.post(
        "/api/v1/compare",
        json={"values": [1.0, "Infinity", -2.0, "-Infinity"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["special_value_policy"]["triggered"] is True
    assert body["special_value_policy"]["result_is_nan"] is True
    assert math.isnan(float("nan"))  # policy renders NaN as the string token
    for method in ("naive", "kahan", "pairwise"):
        assert body["orderings"]["original"]["methods"][method]["result"] == "NaN"


def test_signed_zero_round_trips_through_api(client):
    # Numeric JSON -0.0 carries its sign through parsing and the zero policy.
    resp = client.post("/api/v1/compare", json={"values": [-0.0, -0.0]})
    assert resp.status_code == 200
    result = resp.json()["orderings"]["original"]["methods"]["naive"]["result"]
    # JSON renders -0.0 with its sign.
    assert str(result) == "-0.0"
    assert math.copysign(1.0, float(result)) == -1.0


def test_rejection_empty_input_has_category(client):
    resp = client.post("/api/v1/compare", json={"values": []})
    assert resp.status_code == 422
    error = resp.json()["error"]
    # Pydantic min_length failure category.
    assert "code" in error


def test_rejection_unparseable_token_has_category_and_index(client):
    resp = client.post("/api/v1/compare", json={"values": [1.0, "oops"]})
    assert resp.status_code == 422
    detail = resp.json()["error"]["detail"][0]
    assert "values[1]" in detail["msg"]


def test_rejection_boolean_summand(client):
    resp = client.post("/api/v1/compare", json={"values": [True, 1.0]})
    assert resp.status_code == 422


def test_rejection_unknown_ordering(client):
    resp = client.post(
        "/api/v1/compare", json={"values": [1.0, 2.0], "orderings": ["sideways"]}
    )
    assert resp.status_code == 422
    assert "sideways" in resp.json()["error"]["detail"][0]["msg"]


def test_rejection_bad_block_size(client):
    resp = client.post("/api/v1/compare", json={"values": [1.0], "block_size": 0})
    assert resp.status_code == 422


def test_scenario_endpoint_repeating_decimal(client):
    resp = client.post(
        "/api/v1/scenario",
        json={"scenario": "repeating_decimal", "n": 10, "orderings": ["original"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["scenario"] == {"name": "repeating_decimal", "n": 10}
    methods = body["orderings"]["original"]["methods"]
    assert methods["naive"]["result"] == 0.9999999999999999
    assert methods["kahan"]["result"] == 1.0


def test_scenario_unknown_is_rejected(client):
    resp = client.post("/api/v1/scenario", json={"scenario": "nope", "n": 10})
    assert resp.status_code == 422


def test_input_summary_is_redacted(client):
    resp = client.post(
        "/api/v1/compare",
        json={"values": [12345.678, -0.000091234, 42.0]},
    )
    summary = resp.json()["input_summary"]
    assert summary["redacted"] is True
    # Raw values never appear; only magnitude buckets like +1e4 / -1e-5.
    assert summary["head_magnitude_buckets"] == ["+1e4", "-1e-5", "+1e1"]
    assert "12345.678" not in str(summary)
