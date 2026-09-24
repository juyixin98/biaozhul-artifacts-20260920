"""API-level tests with an isolated temp DB/key, plus crypto checks."""

from __future__ import annotations

import json
import os
import tempfile

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    key = tmp_path / "signing.key"
    monkeypatch.setenv("CLOCKDRIFT_DB", str(db))
    monkeypatch.setenv("CLOCKDRIFT_KEY_FILE", str(key))
    # Fresh singletons per test, closing any connection from a previous test.
    import app.main as main
    if main._store is not None:
        main._store.close()
    main._store = None
    main._service = None
    with TestClient(main.app) as c:
        yield c, key, main
    if main._store is not None:
        main._store.close()
    # Leave singletons null so a later in-process server builds fresh state.
    main._store = None
    main._service = None


def _body(samples, modulus=None, hz=None):
    return {
        "device_id": "d1",
        "counter_modulus": modulus,
        "counter_nominal_hz": hz,
        "samples": samples,
    }


def _linear_samples(n=24, ppm=0.0, hz=1e6):
    """Deterministic, directly-constructed samples (no lab sim)."""
    out = []
    t0_start = 5000.0
    for i in range(n):
        t0 = t0_start + i * 1.0
        t3 = t0 + 0.02
        c_recv = (1 + ppm * 1e-6) * hz * (i * 1.0 + 0.01) + 5.0
        c_send = (1 + ppm * 1e-6) * hz * (i * 1.0 + 0.015) + 5.0
        out.append({
            "t0": t0, "t3": t3,
            "device_counter": c_recv,
            "device_counter_send": c_send,
            "seq": i,
        })
    return out


# --------------------------------------------------------------------------


def test_health(client):
    c, _, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_validation_negative_rtt(client):
    c, _, _ = client
    r = c.post("/api/v1/devices/d1/samples", json=_body([
        {"t0": 10.0, "t3": 9.0, "device_counter": 1.0}]))
    assert r.status_code == 422


def test_calibration_flow_publish_and_versioning(client):
    c, key_path, main = client
    hz = 1e6
    body = _body(_linear_samples(24, ppm=200.0, hz=hz), hz=hz)
    r = c.post("/api/v1/devices/d1/samples?publish=true", json=body)
    assert r.status_code == 200, r.text
    resp = r.json()
    assert resp["status"] == "calibrated"
    assert resp["published"] is True
    assert resp["version"] == "v000001"
    assert len(resp["signature"]) == 64  # hex HMAC-SHA256
    # key file exists with 0600 perms
    assert os.stat(key_path).st_mode & 0o777 == 0o600

    # Convert a counter value inside the evidence range.
    mid = body["samples"][12]
    cv = c.post("/api/v1/convert", json={
        "device_id": "d1",
        "device_counter": mid["device_counter"],
        "host_time_hint": mid["t0"],
    }).json()
    assert cv["status"] == "ok" and cv["version"] == "v000001"
    lo, hi = cv["host_time_interval"]["lower"], cv["host_time_interval"]["upper"]
    assert lo <= cv["host_time_point"] <= hi
    assert hi > lo
    # Error is small for symmetric 20ms RTT.
    assert hi - lo < 0.05

    # Republish closes v1's validity window.
    body2 = _body(_linear_samples(24, ppm=150.0, hz=hz), hz=hz)
    # shift timestamps later so it is genuinely new evidence
    for s in body2["samples"]:
        s["t0"] += 1000.0
        s["t3"] += 1000.0
    r2 = c.post("/api/v1/devices/d1/samples?publish=true", json=body2)
    assert r2.json()["version"] == "v000002"
    models = c.get("/api/v1/devices/d1/models").json()
    assert models[0]["version"] == "v000001" and models[0]["valid_to_host_time"]
    assert models[1]["version"] == "v000002" and models[1]["valid_to_host_time"] is None

    # Pinned historical version still converts.
    old = c.post("/api/v1/convert", json={
        "device_id": "d1", "device_counter": mid["device_counter"],
        "host_time_hint": mid["t0"], "version": "v000001"}).json()
    assert old["version"] == "v000001"


def test_weak_evidence_refuses_publish(client):
    c, _, _ = client
    body = _body(_linear_samples(3, hz=1e6), hz=1e6)
    r = c.post("/api/v1/devices/d1/samples?publish=true", json=body)
    resp = r.json()
    assert resp["status"] == "uncertain"
    assert resp["published"] is False
    assert c.get("/api/v1/models").json() == []


def test_convert_unknown_device(client):
    c, _, _ = client
    r = c.post("/api/v1/convert", json={
        "device_id": "ghost", "device_counter": 1.0})
    assert r.json()["status"] == "uncertain"
    assert "no published calibration" in r.json()["reason"]


def test_observations_remember_version_used(client):
    c, _, main = client
    body = _body(_linear_samples(24, hz=1e6), hz=1e6)
    c.post("/api/v1/devices/d1/samples?publish=true", json=body)
    store = main._store
    rows = store.get_observations("d1")
    assert len(rows) == 24
    # The first batch arrived before any model existed => version_used NULL;
    # a subsequent batch must record v1 as the then-active calibration.
    second = _body(_linear_samples(24, hz=1e6), hz=1e6)
    for s in second["samples"]:
        s["t0"] += 500.0
        s["t3"] += 500.0
    c.post("/api/v1/devices/d1/samples", json=second)
    rows = store.get_observations("d1")
    assert rows[0]["version_used"] is None
    assert all(r["version_used"] == "v000001" for r in rows[24:])


def test_wrap_conversion_across_boundary(client):
    c, _, _ = client
    M = float(2 ** 16)
    hz = 1e6
    samples = []
    # Self-consistent 1 MHz counter: 5 ms spacing => +5000 ticks/sample,
    # 65.5 ms period, so it rolls over twice across 30 samples.
    for i in range(30):
        t0 = 1000.0 + i * 0.005
        raw = (M - 10_000 + i * 5_000) % M
        raw_send = (M - 10_000 + i * 5_000 + 500) % M
        samples.append({
            "t0": t0, "t3": t0 + 0.002,
            "device_counter": raw,
            "device_counter_send": raw_send,
            "seq": i,
        })
    body = _body(samples, modulus=M, hz=hz)
    r = c.post("/api/v1/devices/d1/samples?publish=true", json=body)
    resp = r.json()
    assert resp["status"] == "calibrated", resp
    assert any(d["type"] == "wrap" for d in resp["discontinuities"])
    # Convert a post-wrap raw counter with a hint on the post-wrap side.
    raw_after = float((M - 10_000 + 20 * 5_000) % M)
    cv = c.post("/api/v1/convert", json={
        "device_id": "d1", "device_counter": raw_after,
        "host_time_hint": 1000.0 + 20 * 0.005}).json()
    assert cv["status"] == "ok"


# --------------------------------------------------------------------------
# Crypto
# --------------------------------------------------------------------------


def test_signature_tamper_detection(client):
    c, _, main = client
    body = _body(_linear_samples(24, hz=1e6), hz=1e6)
    c.post("/api/v1/devices/d1/samples?publish=true", json=body)
    # Tamper with the stored signed payload.
    store = main._store
    with store._conn:  # noqa: SLF001 (test-only)
        row = store.get_model("v000001")
        payload = json.loads(row["payload_json"])
        payload["slope"] = payload["slope"] * 1.5
        store._conn.execute(
            "UPDATE models SET payload_json=? WHERE version='v000001'",
            (json.dumps(payload),))
    r = c.get("/api/v1/models/v000001/verify")
    assert r.json()["valid"] is False
    # Conversion of a tampered model must be refused.
    cv = c.post("/api/v1/convert", json={
        "device_id": "d1", "device_counter": 1.0}).json()
    assert cv["status"] == "uncertain"
    assert "FAILED" in cv["reason"]


def test_canonical_json_deterministic():
    from app import crypto
    a = crypto.canonical_json({"b": 1, "a": [1, 2, {"x": 2}]})
    b = crypto.canonical_json({"a": [1, 2, {"x": 2}], "b": 1})
    assert a == b
    key = bytes(range(32))
    sig = crypto.sign({"k": 1}, key)
    assert crypto.verify({"k": 1}, sig, key)
    assert not crypto.verify({"k": 2}, sig, key)


def test_recalibrate_from_history(client):
    c, _, _ = client
    body = _body(_linear_samples(24, hz=1e6), hz=1e6)
    c.post("/api/v1/devices/d1/samples", json=body)
    r = c.post("/api/v1/devices/d1/calibrate?publish=true")
    assert r.status_code == 200
    assert r.json()["published"] is True
