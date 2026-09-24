"""End-to-end HTTP API tests (FastAPI TestClient, no network)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _cluster(ready_delay=2):
    return {
        "generation": 1,
        "nodes": [{"name": "n1"}, {"name": "n2"}, {"name": "n3"}],
        "deployments": [{
            "name": "app", "replicas": 3, "readyDelay": ready_delay,
            "selector": {"matchLabels": {"app": "app"}},
        }],
        "pdbs": [{
            "name": "app-pdb", "minAvailable": 2,
            "selector": {"matchLabels": {"app": "app"}},
        }],
        "pods": [
            {"name": f"app-{i}", "node": f"n{i+1}", "labels": {"app": "app"},
             "owner": {"kind": "ReplicaSet", "name": "app-rs"}, "uid": f"u{i}"}
            for i in range(3)
        ],
    }


def _create_sim(client, snap=None):
    r = client.post("/api/v1/simulations", json={"snapshot": snap or _cluster()})
    assert r.status_code == 201, r.text
    return r.json()["simulationId"]


def test_health_and_root(client):
    assert client.get("/api/v1/healthz").json()["status"] == "ok"
    assert client.get("/").json()["service"]


def test_create_simulation_and_read_snapshot(client):
    sid = _create_sim(client)
    snap = client.get(f"/api/v1/simulations/{sid}/snapshot")
    assert snap.status_code == 200
    assert "etag" in snap.headers
    assert len(snap.json()["pods"]) == 3


def test_unknown_simulation_404(client):
    r = client.get("/api/v1/simulations/nope/snapshot")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_sim"


def test_full_drain_via_autorun_keeps_audit_clean(client):
    sid = _create_sim(client)
    r = client.post(f"/api/v1/simulations/{sid}/drains", json={"nodes": ["n1"]})
    assert r.status_code == 201
    did = r.json()["drainId"]
    token = r.headers["x-plan-token"]
    assert token

    r = client.post(
        f"/api/v1/simulations/{sid}/drains/{did}/autorun",
        json={"maxTicks": 30}, headers={"X-Plan-Token": token},
    )
    assert r.status_code == 200, r.text
    assert r.json()["finalState"] == "COMPLETE"

    audit = client.get(f"/api/v1/simulations/{sid}/audit").json()
    assert audit["violations"] == []
    snap = client.get(f"/api/v1/simulations/{sid}/snapshot").json()
    assert all(p["node"] != "n1" for p in snap["pods"])
    assert sum(1 for p in snap["pods"] if p["ready"]) == 3


def test_plan_token_is_required(client):
    sid = _create_sim(client)
    did = client.post(f"/api/v1/simulations/{sid}/drains",
                      json={"nodes": ["n1"]}).json()["drainId"]
    r = client.post(f"/api/v1/simulations/{sid}/drains/{did}/advance",
                    json={"tickWait": 0})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_token"


def test_tampered_token_rejected(client):
    sid = _create_sim(client)
    r = client.post(f"/api/v1/simulations/{sid}/drains", json={"nodes": ["n1"]})
    did = r.json()["drainId"]
    body, sig = r.headers["x-plan-token"].split(".")
    forged = body + "." + ("0" * len(sig))
    r = client.post(
        f"/api/v1/simulations/{sid}/drains/{did}/advance",
        json={"tickWait": 0}, headers={"X-Plan-Token": forged},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_token"


def test_step_by_step_preconditions_and_replay(client):
    sid = _create_sim(client)
    r = client.post(f"/api/v1/simulations/{sid}/drains", json={"nodes": ["n1"]})
    did = r.json()["drainId"]
    token = r.headers["x-plan-token"]

    # Step 0: cordon.
    r = client.post(f"/api/v1/simulations/{sid}/drains/{did}/advance",
                    json={"tickWait": 0}, headers={"X-Plan-Token": token})
    assert r.status_code == 200
    assert r.json()["executed"] is True
    # Replaying the spent cordon token must fail (step cursor moved).
    replay = client.post(
        f"/api/v1/simulations/{sid}/drains/{did}/advance",
        json={"tickWait": 0}, headers={"X-Plan-Token": token},
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "stale_step"
    token = r.json()["token"]

    # Step 1: wave, then let the replacement mature via explicit ticks.
    r = client.post(f"/api/v1/simulations/{sid}/drains/{did}/advance",
                    json={"tickWait": 0}, headers={"X-Plan-Token": token})
    assert r.json()["executed"] is True
    token = r.json()["token"]

    # Drain cannot complete until replacement is Ready -> WAITING with detail.
    r = client.post(f"/api/v1/simulations/{sid}/drains/{did}/advance",
                    json={"tickWait": 0}, headers={"X-Plan-Token": token})
    assert r.json()["state"] == "WAITING"
    assert not all(p["satisfied"] for p in r.json()["preconditions"])

    r = client.post(f"/api/v1/simulations/{sid}/drains/{did}/advance",
                    json={"tickWait": 5}, headers={"X-Plan-Token": token})
    assert r.json()["state"] == "COMPLETE"


def test_unhealthy_replica_scenario_via_http(client):
    sid = _create_sim(client)
    # app-2 NotReady out of band.
    r = client.post(f"/api/v1/simulations/{sid}/pods/ready",
                    json={"namespace": "default", "name": "app-2", "ready": False})
    assert r.status_code == 200

    created = client.post(f"/api/v1/simulations/{sid}/drains", json={"nodes": ["n1"]})
    did = created.json()["drainId"]
    token = created.headers["x-plan-token"]
    # Cordon.
    token = client.post(
        f"/api/v1/simulations/{sid}/drains/{did}/advance",
        json={"tickWait": 0}, headers={"X-Plan-Token": token},
    ).json()["token"]
    # Wave blocked -> not executed, audit still clean.
    r = client.post(f"/api/v1/simulations/{sid}/drains/{did}/advance",
                    json={"tickWait": 0}, headers={"X-Plan-Token": token})
    body = r.json()
    assert body["executed"] is False and body["state"] == "WAITING"
    assert client.get(f"/api/v1/simulations/{sid}/audit").json()["violations"] == []

    # Heal app-2; old token stale, fetch new via GET.
    client.post(f"/api/v1/simulations/{sid}/pods/ready",
                json={"namespace": "default", "name": "app-2", "ready": True})
    token = client.get(f"/api/v1/simulations/{sid}/drains/{did}").headers["x-plan-token"]
    r = client.post(f"/api/v1/simulations/{sid}/drains/{did}/autorun",
                    json={"maxTicks": 30}, headers={"X-Plan-Token": token})
    assert r.json()["finalState"] == "COMPLETE"
    assert client.get(f"/api/v1/simulations/{sid}/audit").json()["violations"] == []


def test_cancel_scenario_via_http(client):
    sid = _create_sim(client, snap=_cluster(ready_delay=4))
    r = client.post(f"/api/v1/simulations/{sid}/drains", json={"nodes": ["n1"]})
    did = r.json()["drainId"]
    token = r.headers["x-plan-token"]
    token = client.post(
        f"/api/v1/simulations/{sid}/drains/{did}/advance",
        json={"tickWait": 0}, headers={"X-Plan-Token": token},
    ).json()["token"]
    client.post(f"/api/v1/simulations/{sid}/drains/{did}/advance",
                json={"tickWait": 0}, headers={"X-Plan-Token": token})

    cancel = client.post(f"/api/v1/simulations/{sid}/drains/{did}/cancel",
                         headers={"X-Plan-Token": token})
    assert cancel.status_code == 200
    assert cancel.json()["state"] == "CANCELLED"

    again = client.post(f"/api/v1/simulations/{sid}/drains/{did}/advance",
                        json={"tickWait": 5}, headers={"X-Plan-Token": token})
    assert again.status_code == 409 and again.json()["error"]["code"] == "cancelled"


def test_static_pod_blocks_drain(client):
    snap = _cluster()
    snap["pods"][0]["staticPod"] = True
    r = client.post("/api/v1/simulations", json={"snapshot": snap})
    sid = r.json()["simulationId"]
    r = client.post(f"/api/v1/simulations/{sid}/drains", json={"nodes": ["n1"]})
    assert r.json()["state"] == "BLOCKED"
    assert any("static" in b["reason"] for b in r.json()["blockers"])


def test_idempotency_key_returns_same_drain(client):
    sid = _create_sim(client)
    h = {"Idempotency-Key": "abc-123"}
    r1 = client.post(f"/api/v1/simulations/{sid}/drains",
                     json={"nodes": ["n1"]}, headers=h)
    r2 = client.post(f"/api/v1/simulations/{sid}/drains",
                     json={"nodes": ["n1"]}, headers=h)
    assert r1.json()["drainId"] == r2.json()["drainId"]
    assert r2.json()["idempotentReplay"] is True
