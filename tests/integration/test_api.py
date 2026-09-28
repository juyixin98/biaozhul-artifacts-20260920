"""End-to-end API tests over FastAPI's TestClient, asserting concrete
labels, nogoods, statuses and diagnostic records."""

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app


@pytest.fixture()
def client(settings):
    return TestClient(create_app(settings))


@pytest.fixture()
def session(client) -> str:
    resp = client.post("/sessions", json={"session_id": "s1"})
    assert resp.status_code == 201
    return "s1"


def test_full_teaching_scenario(client, session):
    # Shared premise, two independent proofs of C, mutual exclusion A/B.
    assert client.post(f"/sessions/{session}/premises", json={"node": "P"}).status_code == 201
    for name in ("A", "B"):
        resp = client.post(f"/sessions/{session}/assumptions", json={"name": name})
        assert resp.status_code == 201
    rules = [
        {"rule_id": "r1", "antecedents": ["P", "A"], "consequent": "C"},
        {"rule_id": "r2", "antecedents": ["P", "B"], "consequent": "C"},
        {"rule_id": "rx", "antecedents": ["A", "B"], "consequent": "⊥"},
    ]
    for rule in rules:
        assert client.post(f"/sessions/{session}/rules", json=rule).status_code == 201

    # {A,B} is a nogood, but C's environments {A} and {B} stay valid.
    nogoods = client.get(f"/sessions/{session}/nogoods").json()
    assert nogoods == {"nogoods": [["A", "B"]], "complete": True}

    label = client.get(f"/sessions/{session}/nodes/C/label").json()
    assert label["environments"] == [["A"], ["B"]]
    assert label["status"] == "supported"
    assert label["complete"] is True

    # Retracting A must not delete C (still supported by {B}).
    resp = client.delete(f"/sessions/{session}/assumptions/A")
    assert resp.status_code == 200
    label = client.get(f"/sessions/{session}/nodes/C/label").json()
    assert label["environments"] == [["B"]]
    nogoods = client.get(f"/sessions/{session}/nogoods").json()
    assert nogoods["nogoods"] == []


def test_request_id_echoed_and_diagnostics_recorded(client, session):
    resp = client.post(
        f"/sessions/{session}/assumptions",
        json={"name": "A"},
        headers={"X-Request-Id": "req-001"},
    )
    assert resp.headers["X-Request-Id"] == "req-001"

    diagnostics = client.get(f"/sessions/{session}/diagnostics").json()["diagnostics"]
    assert diagnostics, "expected diagnostic records"
    record = diagnostics[-1]
    assert record["request_id"] == "req-001"
    assert record["event"] == "assumption"
    assert record["detail"]["accepted"] is True
    assert record["detail"]["reason"] == "assumption-activated"
    assert "state" in record["detail"]


def test_rejection_explains_why(client, session):
    client.post(f"/sessions/{session}/assumptions", json={"name": "A"})
    resp = client.post(f"/sessions/{session}/assumptions", json={"name": "A"})
    assert resp.status_code == 409
    assert resp.json() == {"accepted": False, "reason": "assumption-already-active"}

    resp = client.delete(f"/sessions/{session}/assumptions/ghost")
    assert resp.status_code == 404


def test_unknown_session_is_404(client):
    assert client.get("/sessions/nope/nogoods").status_code == 404
    resp = client.post("/sessions/nope/premises", json={"node": "P"})
    assert resp.status_code == 404


def test_invalid_rule_payload_is_422(client, session):
    resp = client.post(
        f"/sessions/{session}/rules",
        json={"rule_id": "r1", "antecedents": ["bad name!"], "consequent": "C"},
    )
    assert resp.status_code == 422


def test_session_state_survives_rebuild_from_store(settings):
    # First app instance builds a session; a fresh app over the same
    # database must replay the operation log to identical labels.
    first = TestClient(create_app(settings))
    first.post("/sessions", json={"session_id": "persist"})
    first.post("/sessions/persist/assumptions", json={"name": "A"})
    first.post("/sessions/persist/assumptions", json={"name": "B"})
    first.post(
        "/sessions/persist/rules",
        json={"rule_id": "r1", "antecedents": ["A"], "consequent": "X"},
    )
    first.post(
        "/sessions/persist/rules",
        json={"rule_id": "r2", "antecedents": ["B"], "consequent": "X"},
    )
    first.delete("/sessions/persist/assumptions/A")

    second = TestClient(create_app(settings))
    label = second.get("/sessions/persist/nodes/X/label").json()
    assert label["environments"] == [["B"]]
    assert label["status"] == "supported"


def test_duplicate_session_rejected(client, session):
    resp = client.post("/sessions", json={"session_id": "s1"})
    assert resp.status_code == 409
