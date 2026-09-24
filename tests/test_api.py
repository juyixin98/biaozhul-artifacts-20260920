"""End-to-end HTTP tests via FastAPI TestClient, including real HMAC verification."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.crypto import DEV_DEFAULT_KEY, canonical_json, sign
from app.main import app

from .conftest import load_fixture

client = TestClient(app)


def test_healthz_and_info():
    assert client.get("/healthz").json()["status"] == "ok"
    info = client.get("/api/v1/info").json()
    assert "hmac-sha256" in info["hmacAlgorithm"].lower()
    assert info["implementedFeatures"]


def _analyze_fixture(name: str):
    body = load_fixture(name)["request"]
    return client.post("/api/v1/analyze", json=body)


def test_analyze_fixture_and_signature_roundtrip():
    response = _analyze_fixture("insufficient_resources.json")
    assert response.status_code == 200
    data = response.json()
    assert data["selectedNode"] == "node-b"
    signature = data["signature"]
    covered = {k: v for k, v in data.items() if k not in ("signature", "hmacAlgorithm")}

    # Recompute the signature locally (real HMAC-SHA256) and via the API.
    expected, _ = sign(covered, DEV_DEFAULT_KEY.encode())
    assert signature == expected
    verify = client.post("/api/v1/verify-signature", json={"payload": covered, "signature": signature})
    assert verify.json()["valid"] is True

    # Tampered body must fail verification.
    tampered = json.loads(json.dumps(covered))
    tampered["selectedNode"] = "node-a"
    bad = client.post("/api/v1/verify-signature", json={"payload": tampered, "signature": signature})
    assert bad.json()["valid"] is False


def test_canonical_serialization_is_deterministic():
    payload = {"b": 1, "a": {"z": [1, 2], "y": "x"}}
    assert canonical_json(payload) == canonical_json(json.loads(json.dumps(payload)))


def test_unknown_field_is_rejected():
    body = load_fixture("insufficient_resources.json")["request"]
    body["bogus"] = 123
    response = client.post("/api/v1/analyze", json=body)
    assert response.status_code == 422
    assert response.json()["error"] == "ValidationError"


def test_invalid_quantity_is_rejected():
    body = load_fixture("insufficient_resources.json")["request"]
    body["pod"]["requests"]["cpu"] = "not-a-quantity"
    response = client.post("/api/v1/analyze", json=body)
    assert response.status_code == 422
    assert response.json()["error"] == "InvalidQuantity"


def test_all_four_required_fixture_categories_via_http():
    for name, node, reason in [
        ("insufficient_resources.json", "node-a", "InsufficientResources"),
        ("anti_affinity_conflict.json", "node-a1", "PodAntiAffinityConflict"),
        ("zone_skew.json", "node-a1", "TopologySpreadSkew"),
        ("toleration_match.json", "node-a", "TaintNotTolerated"),
    ]:
        data = _analyze_fixture(name).json()
        entries = {e["node"]: e for e in data["nodes"]}
        assert entries[node]["rejectionReason"] == reason
