"""End-to-end API tests: publish, validity intervals, conversion, history."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.crypto import verify_envelope


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path)
    app.state.now = lambda: 2_000_000_000.0
    with TestClient(app) as c:
        yield c


def _load(name: str) -> dict:
    return json.loads(
        Path("examples").joinpath(f"{name}.json").read_text()
    )


def test_health_exposes_public_key(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["signing_alg"] == "Ed25519"
    assert len(r.json()["public_key"]) > 30


def test_analyze_endpoint_example_shapes(client):
    r = client.post("/api/v1/calibrations/analyze", json=_load("asymmetric"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    seg = body["segments"][0]
    assert seg["fit"]["beta"]["point"] == pytest.approx(1e-3, rel=5e-3)
    # every estimate carries an uncertainty interval
    b = seg["fit"]["beta"]
    assert b["lower"] <= b["point"] <= b["upper"]


def test_few_samples_analyze_is_uncertain(client):
    r = client.post("/api/v1/calibrations/analyze", json=_load("few"))
    assert r.status_code == 200
    # eight samples fit but the uncertainty is too wide for a confident model
    assert r.json()["status"] == "uncertain"


def test_too_few_samples_is_insufficient(client):
    body = _load("asymmetric")
    body = {**body, "samples": body["samples"][:4]}
    r = client.post("/api/v1/calibrations/analyze", json=body)
    assert r.status_code == 200
    assert r.json()["status"] == "insufficient_evidence"


def test_publish_then_convert_uses_signed_version(client):
    payload = _load("asymmetric")
    r = client.post("/api/v1/calibrations/publish", json=payload)
    assert r.status_code == 200, r.text
    env = r.json()["signed_version"]
    ok, _ = verify_envelope(env)
    assert ok

    device = payload["device_id"]
    versions = client.get(f"/api/v1/devices/{device}/versions").json()
    assert len(versions["versions"]) == 1

    # convert a counter in the fitted region
    counter = payload["samples"][-1]["counter"]
    conv = client.post(
        "/api/v1/convert", json={"device_id": device, "counter": counter}
    )
    assert conv.status_code == 200, conv.text
    out = conv.json()
    ht = out["host_time"]
    assert ht["lower"] <= ht["point"] <= ht["upper"]
    assert ht["upper"] - ht["lower"] > 0  # never a fake exact answer
    assert out["version_id"] == env["payload"]["version_id"]

    # history records the version actually used
    hist = client.get(f"/api/v1/devices/{device}/history").json()
    assert len(hist["conversions"]) == 1
    assert hist["conversions"][0]["version_id"] == out["version_id"]


def test_publish_refuses_uncertain_without_force(client):
    r = client.post("/api/v1/calibrations/publish", json=_load("few"))
    assert r.status_code == 409
    forced = client.post(
        "/api/v1/calibrations/publish", json={**_load("few"), "force": True}
    )
    assert forced.status_code == 200


def test_publish_refuses_insufficient_even_with_force_is_409(client):
    body = {**_load("asymmetric"), "samples": _load("asymmetric")["samples"][:4]}
    r = client.post(
        "/api/v1/calibrations/publish", json={**body, "force": True}
    )
    # no feasible segment exists at all; force cannot manufacture one
    assert r.status_code == 409
    assert "feasible" in r.json()["detail"]


def test_validity_intervals_supersede_and_select_versions(client):
    p1 = _load("asymmetric")
    r1 = client.post(
        "/api/v1/calibrations/publish",
        json={**p1, "valid_from": 2_000_000_000.0},
    )
    assert r1.status_code == 200
    v1 = r1.json()["signed_version"]["payload"]["version_id"]

    # a new version starting later closes (supersedes) the open one
    r2 = client.post(
        "/api/v1/calibrations/publish",
        json={**p1, "valid_from": 2_000_001_000.0},
    )
    assert r2.status_code == 200
    v2 = r2.json()["signed_version"]["payload"]["version_id"]

    active_now = client.post(
        "/api/v1/convert",
        json={"device_id": p1["device_id"], "counter": 1.0,
              "at": 2_000_000_500.0},
    ).json()
    assert active_now["version_id"] == v1
    active_later = client.post(
        "/api/v1/convert",
        json={"device_id": p1["device_id"], "counter": 1.0,
              "at": 2_000_001_500.0},
    ).json()
    assert active_later["version_id"] == v2

    # before any version existed -> 404
    early = client.post(
        "/api/v1/convert",
        json={"device_id": p1["device_id"], "counter": 1.0,
              "at": 1_999_999_000.0},
    )
    assert early.status_code == 404


def test_overlapping_closed_intervals_rejected(client):
    p1 = _load("asymmetric")
    client.post(
        "/api/v1/calibrations/publish",
        json={**p1, "valid_from": 100.0, "valid_to": 200.0},
    )
    overlap = client.post(
        "/api/v1/calibrations/publish",
        json={**p1, "valid_from": 150.0, "valid_to": 250.0},
    )
    assert overlap.status_code == 409
    assert "overlap" in overlap.json()["detail"]


def test_verify_endpoint_detects_tampering(client):
    env = client.post(
        "/api/v1/calibrations/publish", json=_load("asymmetric")
    ).json()["signed_version"]
    assert client.post("/api/v1/verify", json=env).json()["valid"] is True
    env["payload"]["model"]["beta_point"] = 999.0
    res = client.post("/api/v1/verify", json=env).json()
    assert res["valid"] is False


def test_unknown_device_conversion_404(client):
    r = client.post(
        "/api/v1/convert", json={"device_id": "ghost", "counter": 1.0}
    )
    assert r.status_code == 404


def test_validation_rejects_recv_before_send(client):
    r = client.post(
        "/api/v1/calibrations/analyze",
        json={
            "device_id": "x",
            "modulus": 10.0,
            "samples": [
                {"t_send": 2.0, "t_recv": 1.0, "counter": 0.0}
            ],
        },
    )
    assert r.status_code == 422
