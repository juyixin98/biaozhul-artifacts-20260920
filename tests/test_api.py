"""API-level tests using FastAPI's TestClient against the example snapshot."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import _store, app

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "snapshot.json"


@pytest.fixture()
def client():
    _store.reset()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def loaded_client(client):
    snapshot = json.loads(EXAMPLE.read_text())
    resp = client.put("/snapshot", json=snapshot)
    assert resp.status_code == 200, resp.text
    return client


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_analyze_before_snapshot_returns_409(client):
    resp = client.post("/analyze", json={
        "source": {"namespace": "frontend", "name": "web-1"},
        "destination": {"namespace": "backend", "name": "api-1"},
        "protocol": "TCP",
        "port": 8080,
    })
    assert resp.status_code == 409


def test_snapshot_load_reports_counts_and_digest(loaded_client):
    info = loaded_client.get("/snapshot").json()
    assert info["pods"] == 5
    assert info["policies"] == 4
    assert len(info["sha256"]) == 64


def _analyze(client, src_ns, src, dst_ns, dst, port, protocol="TCP"):
    return client.post("/analyze", json={
        "source": {"namespace": src_ns, "name": src},
        "destination": {"namespace": dst_ns, "name": dst},
        "protocol": protocol,
        "port": port,
    })


def test_example_web_to_api_reachable_via_named_port(loaded_client):
    resp = _analyze(loaded_client, "frontend", "web-1", "backend", "api-1", "http")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is True
    assert body["port"] == 8080
    assert body["ingress"]["evidence"][0]["policy"] == "backend/allow-web-to-api"


def test_example_web_to_postgres_denied_by_default_deny(loaded_client):
    body = _analyze(loaded_client, "frontend", "web-1", "db", "postgres-1", 5432).json()
    assert body["reachable"] is False
    assert body["ingress"]["isolated"] is True


def test_example_api_to_postgres_reachable(loaded_client):
    body = _analyze(loaded_client, "backend", "api-1", "db", "postgres-1", 5432).json()
    assert body["reachable"] is True
    assert body["egress"]["evidence"][0]["policy"] == "backend/api-egress"
    assert body["ingress"]["evidence"][0]["policy"] == "db/allow-api-to-postgres"


def test_example_ipblock_except(loaded_client):
    excluded = _analyze(loaded_client, "backend", "api-1", "monitoring", "prom-1", 9090).json()
    allowed = _analyze(loaded_client, "backend", "api-1", "monitoring", "prom-2", 9090).json()
    assert excluded["reachable"] is False
    assert excluded["egress"]["allowed"] is False
    assert allowed["reachable"] is True


def test_example_unsupported_protocol(loaded_client):
    body = _analyze(loaded_client, "frontend", "web-1", "backend", "api-1", 8080,
                    protocol="SCTP").json()
    assert body["status"] == "unsupported_protocol"
    assert body["reachable"] is False


def test_unknown_pod_returns_422(loaded_client):
    resp = _analyze(loaded_client, "frontend", "ghost", "backend", "api-1", 80)
    assert resp.status_code == 422
