"""Service-level tests via FastAPI TestClient.

Asserts concrete results AND failure categories through the HTTP boundary,
including request-id propagation and structured error envelopes.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ipwate.api import create_app
from ipwate.config import AppConfig, StorageConfig
from ipwate.synthetic import generate_synthetic


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig(storage=StorageConfig(db_path=str(tmp_path / "api_runs.sqlite3")))
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


def _payload(seed=1, n=1500, **extra):
    data = generate_synthetic(n=n, scenario="good_overlap", seed=seed)
    payload = {
        "x": data.x.tolist(),
        "a": data.a.tolist(),
        "y": data.y.tolist(),
    }
    payload.update(extra)
    return payload, data.ate_true_sample


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["estimand_default"] == "ate"
    assert "request_id" in body
    assert r.headers["x-request-id"]


def test_estimate_success_returns_full_evidence_record(client):
    payload, true_ate = _payload()
    r = client.post("/api/v1/ipw/estimate", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    result = body["result"]
    assert result["verdict"] in ("accept", "warn")
    assert abs(result["estimate"]["point"] - true_ate) < 0.5
    assert result["contract"]["crossfit_n_splits"] == 5
    assert result["contract"]["assumptions"]["conditional_exchangeability"]
    assert "not causal proof" in result["causal_disclaimer"]
    assert len(result["findings"]) >= 0
    assert len(result["folds"]) == 5


def test_request_id_header_is_used_and_returned(client):
    payload, _ = _payload(n=400)
    r = client.post(
        "/api/v1/ipw/estimate", json=payload, headers={"x-request-id": "abc-123"}
    )
    assert r.headers["x-request-id"] == "abc-123"
    assert r.json()["request_id"] == "abc-123"
    assert r.json()["result"]["request_id"] == "abc-123"


def test_explicit_request_id_in_body_takes_precedence(client):
    payload, _ = _payload(n=400)
    payload["request_id"] = "body-id"
    r = client.post("/api/v1/ipw/estimate", json=payload)
    assert r.json()["request_id"] == "body-id"


def test_no_overlap_returns_structured_positivity_error(client):
    data = generate_synthetic(n=1500, scenario="no_overlap", seed=9)
    r = client.post(
        "/api/v1/ipw/estimate",
        json={"x": data.x.tolist(), "a": data.a.tolist(), "y": data.y.tolist()},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "positivity_violation"
    assert "replacement" in body["error"]["message"]
    assert "x-request-id" in r.headers


def test_non_binary_treatment_is_validation_error_category(client):
    payload, _ = _payload(n=100)
    payload["a"][0] = 7
    r = client.post("/api/v1/ipw/estimate", json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_nan_inputs_are_rejected(client):
    payload, _ = _payload(n=100)
    # A standards-compliant client encoder refuses NaN, so inject the raw NaN
    # token into an otherwise valid JSON body to exercise the SERVER boundary.
    payload["x"][0][0] = "__NAN_TOKEN__"
    raw = json.dumps(payload).replace('"__NAN_TOKEN__"', "NaN")
    r = client.post(
        "/api/v1/ipw/estimate",
        content=raw,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_persist_then_fetch_roundtrip(client):
    payload, _ = _payload(n=400)
    payload["request_id"] = "stored-1"
    payload["persist"] = True
    r = client.post("/api/v1/ipw/estimate", json=payload)
    assert r.status_code == 200
    got = client.get("/api/v1/runs/stored-1")
    assert got.status_code == 200
    assert got.json()["result"]["request_id"] == "stored-1"
    missing = client.get("/api/v1/runs/nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_clipping_override_is_reflected_in_contract(client):
    data = generate_synthetic(n=1500, scenario="poor_overlap", seed=11)
    r = client.post(
        "/api/v1/ipw/estimate",
        json={
            "x": data.x.tolist(),
            "a": data.a.tolist(),
            "y": data.y.tolist(),
            "clipping_enabled": True,
        },
    )
    assert r.status_code == 200, r.text
    contract = r.json()["result"]["contract"]
    assert contract["clipping_enabled"] is True
    assert "trimmed" in contract["estimand_note"].lower()


def test_unknown_contract_field_rejected(client):
    payload, _ = _payload(n=200)
    payload["estimand"] = "median"  # pattern validation at schema layer
    r = client.post("/api/v1/ipw/estimate", json=payload)
    assert r.status_code == 422
