"""End-to-end HTTP API tests using FastAPI/Starlette's in-process client."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage import store


SNAPSHOT = {
    "namespaces": [
        {"name": "default", "labels": {}},
        {"name": "dmz", "labels": {"zone": "dmz"}},
    ],
    "pods": [
        {"name": "c", "namespace": "default", "labels": {"app": "c"},
         "ips": ["10.0.0.1"]},
        {"name": "s", "namespace": "default", "labels": {"app": "s"},
         "ips": ["10.0.0.2"],
         "containerPorts": [
             {"name": "http", "containerPort": 8080, "protocol": "TCP"}]},
        {"name": "d", "namespace": "dmz", "labels": {"app": "d"},
         "ips": ["10.4.0.1"]},
    ],
    "policies": [
        {
            "name": "s-deny",
            "namespace": "default",
            "podSelector": {"matchLabels": {"app": "s"}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [{
                "from": [{"podSelector": {"matchLabels": {"app": "c"}}}],
                "ports": [{"port": "http", "protocol": "TCP"}],
            }],
            "egress": [{
                "to": [{"namespaceSelector": {"matchLabels": {"zone": "dmz"}},
                        "podSelector": {"matchLabels": {"app": "d"}}}],
                "ports": [{"port": 53, "protocol": "UDP"}],
            }],
        }
    ],
}


@pytest.fixture(autouse=True)
def _empty_store():
    store._analyzers.clear()
    yield
    store._analyzers.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _upload(client, snapshot):
    resp = client.post("/api/v1/snapshots", json=snapshot)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_snapshot_id_is_real_sha256_of_canonical_json(client):
    body = _upload(client, SNAPSHOT)
    canonical = json.dumps(
        SNAPSHOT, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    expected = hashlib.sha256(canonical).hexdigest()
    assert body["snapshotId"] == expected
    assert body["created"] is True


def test_repost_identical_snapshot_is_idempotent(client):
    first = _upload(client, SNAPSHOT)
    second = _upload(client, copy.deepcopy(SNAPSHOT))
    assert first["snapshotId"] == second["snapshotId"]
    assert second["created"] is False


def test_snapshot_is_immutable_after_upload(client):
    sid = _upload(client, SNAPSHOT)["snapshotId"]
    analyzer = store.get(sid)
    with pytest.raises(Exception):
        analyzer.snapshot.pods[0].name = "changed"  # type: ignore[misc]
    # Stored analyzer objects are not replaced on re-query.
    assert store.get(sid).snapshot.pods[0].name == "c"


def test_list_and_get_snapshot(client):
    sid = _upload(client, SNAPSHOT)["snapshotId"]
    listed = client.get("/api/v1/snapshots").json()["snapshotIds"]
    assert sid in listed
    meta = client.get(f"/api/v1/snapshots/{sid}").json()
    assert meta["pods"] == 3 and meta["policies"] == 1


def test_analyze_full_flow_allowed_and_denied(client):
    sid = _upload(client, SNAPSHOT)["snapshotId"]
    # c -> s named http: ingress allows http; s egress policy only allows
    # UDP/53 to dmz/d, but source of THIS flow is c (egress belongs to c,
    # which has no policy) => non-isolated egress => reachable.
    ok = client.post(f"/api/v1/snapshots/{sid}/analyze", json={
        "source": {"pod": {"namespace": "default", "name": "c"}},
        "destination": {"pod": {"namespace": "default", "name": "s"}},
        "protocol": "TCP", "port": 8080,
    })
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["reachable"] is True
    assert body["ingress"]["allowedBy"][0]["allowedPorts"] == "named:http"

    # Wrong port: ingress does not allow 9090.
    denied = client.post(f"/api/v1/snapshots/{sid}/analyze", json={
        "source": {"pod": {"namespace": "default", "name": "c"}},
        "destination": {"pod": {"namespace": "default", "name": "s"}},
        "protocol": "TCP", "port": 9090,
    }).json()
    assert denied["reachable"] is False
    assert denied["ingress"]["allowed"] is False

    # s -> d TCP/443: s egress only permits UDP/53 to d => egress denies.
    egress_denied = client.post(f"/api/v1/snapshots/{sid}/analyze", json={
        "source": {"pod": {"namespace": "default", "name": "s"}},
        "destination": {"pod": {"namespace": "dmz", "name": "d"}},
        "protocol": "TCP", "port": 443,
    }).json()
    assert egress_denied["reachable"] is False
    assert egress_denied["egress"]["allowed"] is False

    # s -> d UDP/53: egress allows it; d has no ingress policy => reachable.
    udp_ok = client.post(f"/api/v1/snapshots/{sid}/analyze", json={
        "source": {"pod": {"namespace": "default", "name": "s"}},
        "destination": {"pod": {"namespace": "dmz", "name": "d"}},
        "protocol": "UDP", "port": 53,
    }).json()
    assert udp_ok["reachable"] is True


def test_unknown_snapshot_returns_404(client):
    r = client.post("/api/v1/snapshots/deadbeef/analyze", json={})
    assert r.status_code == 404


def test_unsupported_protocol_reported_not_silently_handled(client):
    sid = _upload(client, SNAPSHOT)["snapshotId"]
    r = client.post(f"/api/v1/snapshots/{sid}/analyze", json={
        "source": {"pod": {"namespace": "default", "name": "c"}},
        "destination": {"pod": {"namespace": "default", "name": "s"}},
        "protocol": "SCTP", "port": 5000,
    })
    assert r.status_code == 422
    assert r.json()["error"] == "unsupported_protocol"
    assert "SCTP" in r.json()["detail"]


def test_malformed_snapshot_rejected_with_422(client):
    bad = copy.deepcopy(SNAPSHOT)
    bad["policies"][0]["podSelector"] = {"matchExpressions": [
        {"key": "app", "operator": "Bogus"}]}
    r = client.post("/api/v1/snapshots", json=bad)
    assert r.status_code == 422
    assert r.json()["error"] == "validation_error"


def test_ipblock_except_via_api(client):
    snap = {
        "namespaces": [{"name": "default"}],
        "pods": [{"name": "c", "namespace": "default", "ips": ["10.0.0.1"]}],
        "policies": [{
            "name": "eg", "namespace": "default", "podSelector": {},
            "policyTypes": ["Egress"],
            "egress": [{"to": [{"ipBlock": {
                "cidr": "0.0.0.0/0", "except": ["8.8.8.8/32"]}}]}],
        }],
    }
    sid = _upload(client, snap)["snapshotId"]
    allowed = client.post(f"/api/v1/snapshots/{sid}/analyze", json={
        "source": {"pod": {"namespace": "default", "name": "c"}},
        "destination": {"ip": "1.1.1.1"},
        "protocol": "TCP", "port": 443,
    }).json()
    blocked = client.post(f"/api/v1/snapshots/{sid}/analyze", json={
        "source": {"pod": {"namespace": "default", "name": "c"}},
        "destination": {"ip": "8.8.8.8"},
        "protocol": "TCP", "port": 443,
    }).json()
    assert allowed["reachable"] is True
    assert blocked["reachable"] is False
    assert blocked["egress"]["allowedBy"] == []
