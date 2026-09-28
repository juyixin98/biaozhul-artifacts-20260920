"""End-to-end HTTP tests: API contract, audit trail and logging context."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import SETTINGS
from app.experiments import load_fixture, to_did_request, to_event_request
from app.storage import AuditStore


@pytest.fixture()
def client(tmp_path):
    store = AuditStore(str(tmp_path / "audit.db"))
    app = create_app(store=store)
    return TestClient(app), store


def _did_payload(fixture_name):
    fx = load_fixture(fixture_name)
    return fx, to_did_request(fx).model_dump(mode="json")


def test_did_endpoint_handcalc(client):
    c, _ = client
    fx, payload = _did_payload("handcalc_2x2.json")
    r = c.post("/api/v1/did", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["request_id"] == payload["request_id"]
    assert body["status"] == "ok"
    assert body["version"] and body["service"]
    assert body["decomposition"]["did"] == pytest.approx(2.0)
    assert body["reference_regression"]["did_coefficient"] == pytest.approx(2.0)
    assert "run_id" in body
    # processing location surfaced for interpretability
    assert body["summary"]["processing_location"]


def test_refused_result_is_200_with_explicit_category(client):
    c, _ = client
    fx = load_fixture("event_staggered_refused.json")
    payload = to_event_request(fx).model_dump(mode="json")
    r = c.post("/api/v1/event-study", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "refused"
    assert body["failures"][0]["category"] == "event_stagger_unsupported"


def test_malformed_payload_is_422(client):
    c, _ = client
    r = c.post("/api/v1/did", json={"request_id": "x", "observations": []})
    assert r.status_code == 422


def test_audit_trail_keyed_by_request_identity(client):
    c, store = client
    fx, payload = _did_payload("missing_period.json")
    r = c.post("/api/v1/did", json=payload)
    run_id = r.json()["run_id"]

    row = store.fetch_run(run_id)
    assert row is not None
    assert row["request_id"] == payload["request_id"]
    assert row["endpoint"] == "/api/v1/did"
    saved_req = json.loads(row["request_json"])
    saved_resp = json.loads(row["response_json"])
    assert len(saved_req["observations"]) == len(payload["observations"])
    assert saved_resp["decomposition"]["did"] == pytest.approx(2.0)

    # lookup endpoint by run id and by request_id
    got = c.get(f"/api/v1/runs/{run_id}").json()
    assert got["request_id"] == payload["request_id"]
    listing = c.get("/api/v1/runs", params={"request_id": payload["request_id"]}).json()
    assert listing["count"] >= 1


def test_health(client):
    c, _ = client
    body = c.get("/health").json()
    assert body["status"] == "ok"
    assert body["version"]


def test_excluded_records_are_serialized_with_reasons(client):
    c, _ = client
    fx, payload = _did_payload("missing_period.json")
    body = c.post("/api/v1/did", json=payload).json()
    reasons = {e["object_id"]: e["reasons"] for e in body["excluded_records"]}
    assert reasons["T3_MISSING_POST"] == ["identity_missing_period"]
    # failures and uncertain conclusions are separate sections
    cats = {f["category"] for f in body["failures"]}
    assert "identity_missing_period" in cats
    assert isinstance(body["method_notes"], list) and body["method_notes"]
