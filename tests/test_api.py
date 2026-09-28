"""End-to-end HTTP tests: real success and real failure paths."""
from __future__ import annotations

import pytest

from .conftest import load_fixture


@pytest.mark.integration
def test_health_reports_version(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "alive"
    assert body["engine_version"].startswith("owl-restricted-")


@pytest.mark.integration
def test_reason_success_full_body(client):
    payload = load_fixture("intersection_disjoint_conflict")
    resp = client.post("/reason", json=payload, headers={"X-Request-Id": "abc-123"})
    assert resp.status_code == 200
    body = resp.json()
    # request correlation identity honored
    assert body["request_id"] == "abc-123"
    assert body["consistent"] is False
    assert body["cross_check"]["agreement"] is True
    # conflict path present for the instance
    alice = next(i for i in body["result"]["instances"]
                 if i["instance"] == "alice-7")
    assert alice["status"] == "in_conflict"
    assert set(alice["conflict_path"]["disjoint_classes"]) == {
        "Employee", "Student"
    }


@pytest.mark.integration
def test_reason_unsupported_returns_422_with_precise_error(client):
    payload = load_fixture("unsupported_union")
    resp = client.post("/reason", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["error"]["code"] == "UNSUPPORTED_CONSTRUCTOR"
    assert body["error"]["path"] == "$.axioms[0].super.union"


@pytest.mark.integration
def test_malformed_json_shape_returns_422(client):
    resp = client.post("/reason", json={"not_axioms": []})
    assert resp.status_code == 422  # Pydantic request validation


@pytest.mark.integration
def test_request_audit_trail_endpoint(client):
    payload = load_fixture("equivalence_ring")
    created = client.post(
        "/reason", json=payload, headers={"X-Request-Id": "trace-9"}
    ).json()
    fetched = client.get(f"/requests/{created['request_id']}")
    assert fetched.status_code == 200
    record = fetched.json()
    assert record["engine_version"] == created["engine_version"]
    assert len(record["processing_steps"]) >= 2


@pytest.mark.integration
def test_unknown_request_returns_404(client):
    resp = client.get("/requests/req-does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["failure_categories"] == ["REQUEST_NOT_FOUND"]


@pytest.mark.integration
def test_subclass_route(client):
    payload = load_fixture("multi_inheritance")
    resp = client.post("/subclass", json={
        **payload, "sub": "EmperorPenguin", "super": "Bird"
    })
    assert resp.status_code == 200
    assert resp.json()["entailed"] is True


@pytest.mark.integration
def test_log_file_written_with_version(tmp_settings, client):
    payload = load_fixture("two_assertions_conflict")
    client.post("/reason", json=payload)
    log_text = tmp_settings.log_path.read_text(encoding="utf-8")
    assert "owl-restricted-" in log_text
    assert "classify" in log_text
