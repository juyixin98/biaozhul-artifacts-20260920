"""End-to-end HTTP interface tests (FastAPI + httpx TestClient)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from reteapp.api import create_app
from reteapp.config import load_settings
from tests.helpers import load_fixture


def _client() -> TestClient:
    settings = load_settings(db_path=":memory:", max_fire_rounds=10)
    return TestClient(create_app(settings=settings))


def _create_run(client: TestClient, fixture: str = "rules_orders.json", run_id: str = "api-1") -> dict:
    response = client.post("/runs", json={"rules": load_fixture(fixture), "run_id": run_id})
    assert response.status_code == 201, response.text
    return response.json()


def test_health_and_version() -> None:
    client = _client()
    assert client.get("/health").json()["status"] == "ok"
    version = client.get("/version").json()
    assert version["version"]
    assert version["settings"]["max_fire_rounds"] == 10


def test_full_lifecycle_agenda_sources_and_fire() -> None:
    client = _client()
    created = _create_run(client)
    assert created["run_id"] == "api-1"

    r1 = client.post("/runs/api-1/facts", json={
        "fact": {"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}}
    })
    assert r1.status_code == 201
    assert r1.json()["wme_id"] == 1
    r2 = client.post("/runs/api-1/facts", json={
        "fact": {"type": "Order", "fields": {"customer": "c1", "amount": 250}}
    })
    assert r2.json()["wme_id"] == 2

    agenda = client.get("/runs/api-1/agenda").json()
    assert agenda["count"] == 2
    top = agenda["agenda"][0]
    # Highest salience first, with explicit per-condition sources.
    assert top["rule"] == "gold_customer_order"
    assert top["stable_key"] == "gold_customer_order[1-2]"
    assert {s["ce_index"] for s in top["sources"]} == {0, 1}
    assert top["sources"][0]["type"] == "Customer"
    assert top["sources"][1]["fields"]["amount"] == 250

    fired = client.post("/runs/api-1/fire", json={"mode": "all"})
    body = fired.json()
    assert body["status"] == "fired"
    assert body["bounded"] is True
    assert [f["rule"] for f in body["fired"]] == [
        "gold_customer_order", "customer_order_any_tier"
    ]
    # The asserted Discount fact is visible in working memory.
    facts = client.get("/runs/api-1/facts").json()["facts"]
    assert any(f["type"] == "Discount" for f in facts)


def test_retract_removes_dependent_activations() -> None:
    client = _client()
    _create_run(client)
    client.post("/runs/api-1/facts", json={
        "fact": {"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}}})
    client.post("/runs/api-1/facts", json={
        "fact": {"type": "Order", "fields": {"customer": "c1", "amount": 250}}})
    deleted = client.delete("/runs/api-1/facts/2")
    assert deleted.status_code == 200
    assert client.get("/runs/api-1/agenda").json()["count"] == 0


def test_bounded_fire_reports_limit_reached_not_success_loop() -> None:
    client = _client()
    _create_run(client, fixture="rules_loop.json", run_id="loop-1")
    client.post("/runs/loop-1/facts", json={"fact": {"type": "Counter", "fields": {"n": 0}}})
    body = client.post("/runs/loop-1/fire", json={"mode": "all"}).json()
    assert body["status"] == "limit_reached"
    assert body["rounds_used"] == 10
    assert body["remaining_activations"] == 1


def test_error_categories_are_explicit_never_generic_success() -> None:
    client = _client()
    # Malformed rules -> session_error category.
    bad = client.post("/runs", json={"rules": [{"name": "x"}]})
    assert bad.status_code == 400
    assert bad.json()["error"]["category"] == "session_error"

    _create_run(client, run_id="err-1")
    # Invalid fact payload -> fact_validation_error (422).
    invalid = client.post("/runs/err-1/facts", json={"fact": {"fields": {"x": 1}}})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["category"] == "fact_validation_error"

    # Retract of unknown WME -> unknown_fact_error (404).
    missing = client.delete("/runs/err-1/facts/42")
    assert missing.status_code == 404
    assert missing.json()["error"]["category"] == "unknown_fact_error"

    # Unknown run -> unknown_session_error (404).
    assert client.get("/runs/no-such-run").status_code == 404
    assert client.get("/runs/no-such-run").json()["error"]["category"] == "unknown_session_error"

    # Schema-level failure -> request_validation_error (422), not 500/200.
    schema_fail = client.post("/runs/err-1/fire", json={"mode": "turbo"})
    assert schema_fail.status_code == 422
    assert schema_fail.json()["error"]["category"] == "request_validation_error"


def test_request_id_and_version_headers_correlate() -> None:
    client = _client()
    response = client.get("/health", headers={"X-Request-ID": "corr-123"})
    assert response.headers["X-Request-ID"] == "corr-123"
    assert response.headers["X-Engine-Version"]


def test_trace_and_evidence_and_network_endpoints() -> None:
    client = _client()
    _create_run(client)
    client.post("/runs/api-1/facts", json={
        "fact": {"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}}})
    trace = client.get("/runs/api-1/trace").json()["trace"]
    assert any(row["event"] == "activation_queued" for row in trace) or trace

    network = client.get("/runs/api-1/network").json()["network"]
    assert network["wme_count"] == 1
    assert any(m["type"] == "Customer" for m in network["alpha_memories"])

    evidence = client.get("/runs/api-1/evidence").json()["evidence"]
    assert evidence["facts"].get("insert") == 1

    runs = client.get("/runs").json()["runs"]
    assert any(r["run_id"] == "api-1" for r in runs)
