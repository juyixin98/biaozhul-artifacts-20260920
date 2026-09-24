"""General API behaviour: health, CRUD, validation, stateless estimate,
example-data acceptance."""
import json
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["param_signature_verified"] is True


def test_session_crud(client):
    assert client.get("/v1/sessions/unknown").status_code == 404
    r = client.post("/v1/sessions", json={"session_id": "x", "initial_soc": 0.4})
    assert r.status_code == 201
    assert client.post("/v1/sessions", json={"session_id": "x"}).status_code == 409
    assert "x" in client.get("/v1/sessions").json()["sessions"]
    assert client.delete("/v1/sessions/x").status_code == 200
    assert client.get("/v1/sessions/x").status_code == 404


def test_ingest_unknown_session_404(client):
    r = client.post("/v1/sessions/ghost/samples", json={"samples": [
        {"t_s": 0.0, "current_a": 1.0, "voltage_v": 3.8, "temp_c": 25.0}
    ]})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SESSION_NOT_FOUND"


def test_validation_rejects_bad_samples(client):
    client.post("/v1/sessions", json={"session_id": "v"})
    # voltage out of range
    r = client.post("/v1/sessions/v/samples", json={"samples": [
        {"t_s": 0.0, "current_a": 1.0, "voltage_v": 12.0, "temp_c": 25.0}
    ]})
    assert r.status_code == 422
    # NaN current (raw JSON, since NaN is not valid JSON either)
    r = client.post(
        "/v1/sessions/v/samples",
        content='{"samples": [{"t_s": 0.0, "current_a": NaN, "voltage_v": 3.8, "temp_c": 25.0}]}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
    # missing field
    r = client.post("/v1/sessions/v/samples", json={"samples": [{"t_s": 0.0}]})
    assert r.status_code == 422


def test_stateless_estimate_matches_engine(client, params):
    from app.engine import Sample, estimate

    samples = [
        {"t_s": float(t), "current_a": 5.0, "voltage_v": 3.8, "temp_c": 25.0}
        for t in range(0, 100)
    ]
    r = client.post("/v1/estimate", json={"initial_soc": 0.5, "samples": samples})
    assert r.status_code == 200
    body = r.json()
    direct = estimate(
        [Sample(s["t_s"], s["current_a"], s["voltage_v"], s["temp_c"]) for s in samples],
        params, initial_soc=0.5,
    )
    assert body["summary"]["soc"] == pytest.approx(direct["summary"]["soc"], abs=1e-15)
    assert body["summary"]["param_version"] == params.version


def test_recompute_endpoint_anchor_verified(client):
    client.post("/v1/sessions", json={"session_id": "rc", "initial_soc": 0.5})
    client.post("/v1/sessions/rc/samples", json={"samples": [
        {"t_s": float(t), "current_a": 2.0, "voltage_v": 3.8, "temp_c": 25.0}
        for t in range(0, 50)
    ]})
    r = client.post("/v1/sessions/rc/recompute")
    assert r.status_code == 200
    assert r.json()["anchor_verified"] is True


def test_example_telemetry_end_to_end(client):
    """Acceptance: the committed example file exercises calibration, gap,
    out-of-table temperature, charge and discharge."""
    client.post("/v1/sessions", json=json.loads((EXAMPLES / "create_session_demo.json").read_text()))
    payload = json.loads((EXAMPLES / "sample_telemetry.json").read_text())
    r = client.post("/v1/sessions/demo/samples", json=payload)
    assert r.status_code == 200
    summary = r.json()["summary"]
    assert summary["n_samples"] == len(payload["samples"])
    assert summary["n_gaps"] == 1
    assert summary["n_ocv_calibrations"] > 0
    assert summary["n_temp_out_of_table"] == 20
    assert summary["soc"] == pytest.approx(0.6, abs=1e-9)  # final rest @ 3.78 V
    assert summary["sigma"] == pytest.approx(0.01, abs=1e-12)
    # trace endpoint paginates
    tr = client.get("/v1/sessions/demo/trace", params={"offset": 0, "limit": 5}).json()
    assert tr["total"] == summary["n_samples"] and len(tr["trace"]) == 5
    # events endpoint lists the gap and calibrations
    events = client.get("/v1/sessions/demo/events").json()["events"]
    assert any(e["type"] == "gap" for e in events)
    assert any(e["type"] == "ocv_calibration" for e in events)
    # evidence chain valid
    assert client.get("/v1/sessions/demo/evidence").json()["chain_valid"] is True
