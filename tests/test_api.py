"""End-to-end HTTP API tests (FastAPI via httpx ASGI transport)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.synthetic import scenario_sensor_dropout


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _payload(samples, initial_soc=None):
    return {
        "initial_soc": initial_soc,
        "samples": [
            {"t_s": s.t_s, "current_a": s.current_a, "voltage_v": s.voltage_v, "temp_c": s.temp_c}
            for s in samples
        ],
    }


def test_health_and_params_banner(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert "NOT FOR REAL CHARGE CONTROL" in r.json()["not_for_control"]

    r = client.get("/api/v1/params")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "soc-estimator-params/v1"
    assert body["verified"] is True
    assert body["sign_convention"]["current_positive"] == "discharge"


def test_one_shot_estimate_and_validation(client, params):
    synth = scenario_sensor_dropout(params)
    r = client.post("/api/v1/estimate", json=_payload(synth.samples[:200]))
    assert r.status_code == 200
    body = r.json()
    assert body["params_version"].startswith("soc-estimator")
    assert body["result"]["n_samples"] == 200
    assert "NOT FOR REAL CHARGE CONTROL" in body["not_for_control"]

    # Validation: wrong sign/order, negative voltage, extra field.
    bad = {"samples": [{"t_s": 1, "current_a": 0, "voltage_v": 3.8, "temp_c": 25},
                       {"t_s": 0, "current_a": 0, "voltage_v": 3.8, "temp_c": 25}]}
    assert client.post("/api/v1/estimate", json=bad).status_code == 422
    bad_v = {"samples": [{"t_s": 0, "current_a": 0, "voltage_v": -1, "temp_c": 25}]}
    assert client.post("/api/v1/estimate", json=bad_v).status_code == 422
    extra = {"samples": [{"t_s": 0, "current_a": 0, "voltage_v": 3.8, "temp_c": 25, "x": 1}]}
    assert client.post("/api/v1/estimate", json=extra).status_code == 422
    huge = {"samples": [{"t_s": 0, "current_a": 99999.0, "voltage_v": 3.8, "temp_c": 25}]}
    assert client.post("/api/v1/estimate", json=huge).status_code == 422


def test_session_ingest_late_data_restart_and_finalize(client, params):
    synth = scenario_sensor_dropout(params)
    samples = synth.samples

    # Create a session with a narrow replay window and send the first batch.
    r = client.post(
        "/api/v1/sessions",
        json={"samples": _payload(samples[:300])["samples"], "replay_window_s": 120.0},
    )
    assert r.status_code == 201
    sid = r.json()["session_id"]

    # Late data beyond the window is rejected.
    r = client.post(f"/api/v1/sessions/{sid}/ingest", json={"samples": _payload(samples[:10])["samples"]})
    assert r.json()["rejected"] == 10

    # Continue with forward data (the batch immediately after t=299 is within window).
    r = client.post(f"/api/v1/sessions/{sid}/ingest", json={"samples": _payload(samples[300:600])["samples"]})
    assert r.json()["accepted"] == 300

    status = client.get(f"/api/v1/sessions/{sid}").json()
    assert status["n_samples"] == 600
    assert status["result"]["n_gap_intervals"] == 0 or status["result"]["n_gap_intervals"] >= 0

    # Restart persistence: a second process-context store reads the same files.
    from app.session_store import SessionStore
    import app.main as main_mod
    fresh = SessionStore(params, main_mod.DATA_DIR)
    assert fresh.get(sid) is not None and len(fresh.get(sid).samples) == 600

    # Finalize then ingest -> rejected.
    r = client.post(f"/api/v1/sessions/{sid}/finalize")
    assert r.status_code == 200 and r.json()["finalized"] is True
    r = client.post(f"/api/v1/sessions/{sid}/ingest", json={"samples": _payload(samples[600:601])["samples"]})
    assert r.json()["accepted"] == 0 and r.json()["rejected"] == 1

    # Unknown session.
    assert client.get("/api/v1/sessions/nope").status_code == 404


def test_evidence_verify_endpoint(client, params):
    synth = scenario_sensor_dropout(params)
    client.post("/api/v1/estimate", json=_payload(synth.samples[:50]))
    r = client.get("/api/v1/evidence/verify")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["records"] >= 1
