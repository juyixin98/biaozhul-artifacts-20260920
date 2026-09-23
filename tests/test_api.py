"""HTTP-level tests using FastAPI's in-process ASGI client."""

from __future__ import annotations

import numpy as np
import pytest

from fastapi.testclient import TestClient

from imu_bias_estimator.app import app

from .synth import make_static, add_motion

client = TestClient(app)


def _payload(t, a, g, temp=None, units=None):
    p = {
        "timestamps": t.tolist(),
        "accelerometer": a.tolist(),
        "gyroscope": g.tolist(),
    }
    if temp is not None:
        p["temperature"] = temp.tolist()
    if units is not None:
        p["units"] = units
    return p


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_observability_endpoint_states_limitations():
    r = client.get("/api/v1/observability")
    assert r.status_code == 200
    body = r.json()
    assert body["gyroscope_bias"]["observable"] is True
    assert body["accelerometer_bias"]["observable"] is False


def test_estimate_endpoint_static():
    t, a, g = make_static(duration=8.0, bias=(0.01, -0.005, 0.002), seed=1)
    r = client.post("/api/v1/estimate", json=_payload(t, a, g))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    est = np.array(body["gyroscope_bias"]["estimate_radps"])
    np.testing.assert_allclose(est, [0.01, -0.005, 0.002], atol=1e-3)


def test_estimate_with_sudden_motion():
    t, a, g = make_static(duration=24.0, bias=(0.01, -0.005, 0.002), seed=2)
    add_motion(t, a, g, 6.0, 18.0)
    r = client.post("/api/v1/estimate", json=_payload(t, a, g))
    assert r.status_code == 200
    body = r.json()
    assert body["anomalies"]["sudden_motion"]["detected"] is True
    est = np.array(body["gyroscope_bias"]["estimate_radps"])
    np.testing.assert_allclose(est, [0.01, -0.005, 0.002], atol=1.5e-3)


def test_estimate_rejects_bad_shapes():
    r = client.post(
        "/api/v1/estimate",
        json={
            "timestamps": [0.0, 0.1],
            "accelerometer": [[0, 0, 9.8]],
            "gyroscope": [[0, 0, 0], [0, 0, 0]],
        },
    )
    assert r.status_code == 422


def test_estimate_rejects_nonfinite_and_duplicates_policy_error():
    t, a, g = make_static(duration=2.0, seed=3)
    t = np.append(t, t[-1])
    a = np.vstack([a, a[-1]])
    g = np.vstack([g, g[-1]])
    r = client.post(
        "/api/v1/estimate",
        json={**_payload(t, a, g), "time_repeat_policy": "error"},
    )
    assert r.status_code == 422
    assert "duplicate" in r.json()["detail"]

    # explicit JSON text so the NaN token reaches the service (json= would
    # raise client-side with strict parsers)
    payload = _payload(t, a, g)
    payload["gyroscope"][3][0] = "NaN"
    r = client.post("/api/v1/estimate", json=payload)
    assert r.status_code == 422


def test_no_stationary_payload_still_200_with_explicit_status():
    rng = np.random.default_rng(0)
    t = np.arange(800) / 100.0
    payload = _payload(t, rng.normal(0, 3, (800, 3)), rng.normal(0, 1, (800, 3)))
    r = client.post("/api/v1/estimate", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "no_stationary_data"
    assert body["gyroscope_bias"] is None
    assert body["candidates"] == []
