"""Late data: replay only inside the configured window; stale data rejected;
recompute is deterministic (same result as a single-shot ingest)."""
import pytest


def _rest_samples(t0, t1, step=10.0):
    n = int((t1 - t0) / step) + 1
    return [
        {"t_s": t0 + k * step, "current_a": 0.0, "voltage_v": 3.78, "temp_c": 25.0}
        for k in range(n)
    ]


def test_late_sample_inside_window_accepted_and_deterministic(client):
    client.post("/v1/sessions", json={"session_id": "a", "initial_soc": 0.6})
    base = _rest_samples(0.0, 4000.0)
    r = client.post("/v1/sessions/a/samples", json={"samples": base})
    assert r.status_code == 200
    soc_before = r.json()["summary"]["soc"]

    # late sample inside the 3600 s replay window (floor = 4000 - 3600 = 400)
    r = client.post("/v1/sessions/a/samples", json={
        "samples": [{"t_s": 2005.0, "current_a": 0.0, "voltage_v": 3.78, "temp_c": 25.0}]
    })
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 1 and body["recomputed"] and body["anchor_verified"]

    # determinism: a fresh session ingesting everything at once matches exactly
    client.post("/v1/sessions", json={"session_id": "b", "initial_soc": 0.6})
    merged = base + [{"t_s": 2005.0, "current_a": 0.0, "voltage_v": 3.78, "temp_c": 25.0}]
    r = client.post("/v1/sessions/b/samples", json={"samples": merged})
    assert r.status_code == 200
    one_shot = r.json()["summary"]
    replayed = client.get("/v1/sessions/a/soc").json()
    assert replayed["soc"] == pytest.approx(one_shot["soc"], abs=1e-15)
    assert replayed["sigma"] == pytest.approx(one_shot["sigma"], abs=1e-15)
    assert replayed["soc"] == pytest.approx(soc_before, abs=1e-15)  # rest OCV dominates


def test_stale_sample_rejected_with_409(client):
    client.post("/v1/sessions", json={"session_id": "s", "initial_soc": 0.6})
    client.post("/v1/sessions/s/samples", json={"samples": _rest_samples(0.0, 4000.0)})
    r = client.post("/v1/sessions/s/samples", json={
        "samples": [{"t_s": 105.0, "current_a": 0.0, "voltage_v": 3.78, "temp_c": 25.0}]
    })
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "STALE_DATA"
    assert err["stale_timestamps"] == [105.0]
    # state untouched
    assert client.get("/v1/sessions/s").json()["n_samples"] == 401


def test_mixed_late_and_stale_partial_accept(client):
    client.post("/v1/sessions", json={"session_id": "m", "initial_soc": 0.6})
    client.post("/v1/sessions/m/samples", json={"samples": _rest_samples(0.0, 4000.0)})
    r = client.post("/v1/sessions/m/samples", json={"samples": [
        {"t_s": 2005.0, "current_a": 0.0, "voltage_v": 3.78, "temp_c": 25.0},
        {"t_s": 105.0, "current_a": 0.0, "voltage_v": 3.78, "temp_c": 25.0},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 1
    assert body["stale_rejected"] == [105.0]


def test_duplicate_timestamps_ignored(client):
    client.post("/v1/sessions", json={"session_id": "d", "initial_soc": 0.6})
    samples = _rest_samples(0.0, 100.0)
    client.post("/v1/sessions/d/samples", json={"samples": samples})
    r = client.post("/v1/sessions/d/samples", json={"samples": samples})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 0
    assert body["duplicates"] == len(samples)
    assert body["recomputed"] is False
