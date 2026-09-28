"""End-to-end service and HTTP/API tests, including persistence and privacy."""

from __future__ import annotations

import pytest

from app.api import create_app
from app.dgp import DGPSpec
from app.storage import DecisionStore
from fastapi.testclient import TestClient


@pytest.fixture
def client(config):
    store = DecisionStore(config.absolute_db_path)
    app = create_app(config=config, store=store)
    with TestClient(app) as c:
        c._store = store
        yield c


@pytest.mark.service
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.service
def test_estimate_strong_instruments_accepted_and_persisted(client, build_request):
    spec = DGPSpec(n=4000, n_instruments=2, instrument_strength=0.8, seed=407)
    req = build_request(spec).model_dump()
    r = client.post("/api/v1/estimate", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    rid = body["request_id"]
    assert body["status"] == "estimated"
    assert body["decision"]["verdict"] == "accepted"
    assert body["decision"]["failure_category"] == "none"
    assert r.headers["x-request-id"] == rid

    # Persisted under the same correlation id, without raw data.
    stored = client._store.fetch(rid)
    assert stored is not None
    assert stored["verdict"] == "accepted"
    assert "columns" not in stored
    assert len(stored["fingerprint"]) == 12


@pytest.mark.service
def test_estimate_weak_returns_200_with_inconclusive_not_http_error(
    client, build_request
):
    spec = DGPSpec(n=3000, n_instruments=2, instrument_strength=0.02, seed=402)
    r = client.post("/api/v1/estimate", json=build_request(spec).model_dump())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "estimated_weak"
    assert body["decision"]["verdict"] == "inconclusive"
    assert body["decision"]["failure_category"] == "weak_instruments"


@pytest.mark.service
def test_underidentified_returns_typed_422_envelope(client, build_request):
    spec = DGPSpec(n=2000, n_endogenous=2, n_instruments=1, seed=403)
    r = client.post("/api/v1/estimate", json=build_request(spec).model_dump())
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "unidentified_model"
    assert err["details"]["n_excluded_instruments"] == 1
    assert err["request_id"]


@pytest.mark.service
def test_schema_error_envelope_on_bad_payload(client):
    r = client.post(
        "/api/v1/estimate",
        json={"dependent": "y", "endogenous": [], "instruments": [],
              "columns": {}, "assume_exclusion_restriction": True},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


@pytest.mark.service
def test_run_lookup_and_listing(client, build_request):
    spec = DGPSpec(n=2000, n_instruments=2, seed=404)
    payload = build_request(spec, request_id="fixed-correlation-id").model_dump()
    r = client.post("/api/v1/estimate", json=payload)
    assert r.status_code == 200

    got = client.get("/api/v1/runs/fixed-correlation-id")
    assert got.status_code == 200
    assert got.json()["request_id"] == "fixed-correlation-id"

    missing = client.get("/api/v1/runs/nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"

    listing = client.get("/api/v1/runs")
    assert listing.status_code == 200
    assert any(row["request_id"] == "fixed-correlation-id"
               for row in listing.json()["runs"])


@pytest.mark.service
def test_request_id_round_trips_via_header(client, build_request):
    spec = DGPSpec(n=1500, n_instruments=2, seed=405)
    r = client.post(
        "/api/v1/estimate",
        json=build_request(spec).model_dump(),
        headers={"X-Request-ID": "header-correlation-123"},
    )
    assert r.status_code == 200
    assert r.json()["request_id"] == "header-correlation-123"
    assert r.headers["x-request-id"] == "header-correlation-123"
