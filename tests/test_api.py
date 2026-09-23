"""End-to-end HTTP tests: HMAC-SHA256 auth, replay idempotency/conflict,
out-of-order rejection, empty frames and duplicate detections over the wire."""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.service import store


@pytest.fixture
def client():
    store.reset()
    return TestClient(app)


def _sign(client_method: str, path: str, body: bytes, secret: str, ts: str | None = None):
    ts = ts or f"{time.time():.3f}"
    digest = hashlib.sha256(body).hexdigest()
    msg = f"{client_method}\n{path}\n{ts}\n{digest}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return {"X-Session-Id": path.split("/")[2], "X-Timestamp": ts, "X-Signature": sig}


@pytest.fixture
def session(client):
    r = client.post("/sessions", json={"tracker": {"hits_to_confirm": 2, "r": 0.0001}})
    assert r.status_code == 201
    data = r.json()
    return data["session_id"], data["secret"]


def _frame(fid, ts, dets):
    return {"frame_id": fid, "timestamp": ts, "detections": dets}


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_missing_signature_headers_rejected(client, session):
    sid, _ = session
    r = client.post(f"/sessions/{sid}/frames", json=_frame(0, 0.0, []))
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "missing_credentials"


def test_bad_signature_rejected(client, session):
    sid, secret = session
    path = f"/sessions/{sid}/frames"
    body = json.dumps(_frame(0, 0.0, [])).encode()
    h = _sign("POST", path, body, secret)
    h["X-Signature"] = "00" * 32
    r = client.post(path, content=body, headers=h)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "bad_signature"


def test_timestamp_skew_rejected(client, session):
    sid, secret = session
    path = f"/sessions/{sid}/frames"
    body = json.dumps(_frame(0, 0.0, [])).encode()
    old = f"{time.time() - 10_000:.3f}"
    h = _sign("POST", path, body, secret, ts=old)
    r = client.post(path, content=body, headers=h)
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "timestamp_skew"


def test_body_tampering_invalidates_signature(client, session):
    sid, secret = session
    path = f"/sessions/{sid}/frames"
    body = json.dumps(_frame(0, 0.0, [])).encode()
    h = _sign("POST", path, body, secret)
    tampered = json.dumps(_frame(0, 0.0, [{"x": 9, "y": 9}])).encode()
    r = client.post(path, content=tampered, headers=h)
    assert r.status_code == 403


def test_signed_tracking_flow_and_fields(client, session):
    sid, secret = session
    path = f"/sessions/{sid}/frames"
    # frame 0: one detection -> tentative track
    body = json.dumps(_frame(0, 0.0, [{"x": 0, "y": 0, "label": "d0"}])).encode()
    r = client.post(path, content=body, headers=_sign("POST", path, body, secret))
    assert r.status_code == 200, r.text
    assert r.json()["new_tracks"] == [1]
    # frame 1: matched -> confirmed (hits_to_confirm=2), full report present
    body = json.dumps(_frame(1, 0.1, [{"x": 1, "y": 0}])).encode()
    out = client.post(path, content=body, headers=_sign("POST", path, body, secret)).json()
    assert out["confirmed_tracks"] == [1]
    a = out["associations"][0]
    assert set(a) >= {
        "track_id",
        "detection_index",
        "prediction",
        "measurement",
        "mahalanobis_sq",
        "euclidean",
        "gate_threshold",
        "in_gate",
        "selection_basis",
    }
    assert a["in_gate"] is True
    assert out["dt"] == pytest.approx(0.1)


def test_repeated_frame_is_idempotent_and_does_not_add_id(client, session):
    sid, secret = session
    path = f"/sessions/{sid}/frames"
    payload = _frame(0, 0.0, [{"x": 0, "y": 0}])
    body = json.dumps(payload).encode()
    h1 = _sign("POST", path, body, secret)
    first = client.post(path, content=body, headers=h1).json()
    # Fresh signature for the identical re-delivery (real replay at the wire).
    h2 = _sign("POST", path, body, secret)
    second = client.post(path, content=body, headers=h2)
    assert second.status_code == 200
    assert second.json() == first
    state = client.get(
        f"/sessions/{sid}", headers=_sign("GET", f"/sessions/{sid}", b"", secret)
    ).json()
    assert state["track_count"] == 1


def test_repeated_frame_with_different_body_conflicts(client, session):
    sid, secret = session
    path = f"/sessions/{sid}/frames"
    b1 = json.dumps(_frame(0, 0.0, [{"x": 0, "y": 0}])).encode()
    client.post(path, content=b1, headers=_sign("POST", path, b1, secret))
    b2 = json.dumps(_frame(0, 0.0, [{"x": 5, "y": 5}])).encode()
    r = client.post(path, content=b2, headers=_sign("POST", path, b2, secret))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "replay_body_mismatch"


def test_out_of_order_frame_rejected(client, session):
    sid, secret = session
    path = f"/sessions/{sid}/frames"
    for fid, ts in [(1, 0.1), (2, 0.2)]:
        body = json.dumps(_frame(fid, ts, [])).encode()
        assert client.post(path, content=body, headers=_sign("POST", path, body, secret)).status_code == 200
    body = json.dumps(_frame(1, 0.3, [])).encode()
    r = client.post(path, content=body, headers=_sign("POST", path, body, secret))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "frame_out_of_order"


def test_duplicate_detections_over_wire(client, session):
    sid, secret = session
    path = f"/sessions/{sid}/frames"
    payload = _frame(0, 0.0, [{"x": 1, "y": 1}, {"x": 1.01, "y": 1.0}])
    body = json.dumps(payload).encode()
    out = client.post(path, content=body, headers=_sign("POST", path, body, secret)).json()
    assert out["new_tracks"] == [1]
    assert len(out["duplicate_detections"]) == 1


def test_empty_frame_keeps_tracks_and_reports_misses(client, session):
    sid, secret = session
    path = f"/sessions/{sid}/frames"
    b0 = json.dumps(_frame(0, 0.0, [{"x": 0, "y": 0}])).encode()
    b1 = json.dumps(_frame(1, 0.1, [{"x": 1, "y": 0}])).encode()
    b2 = json.dumps(_frame(2, 0.2, [])).encode()
    for b in (b0, b1):
        client.post(path, content=b, headers=_sign("POST", path, b, secret))
    out = client.post(path, content=b2, headers=_sign("POST", path, b2, secret)).json()
    assert out["associations"] == []
    assert [t["track_id"] for t in out["unmatched_tracks"]] == [1]
    assert out["unmatched_tracks"][0]["misses"] == 1


def test_unknown_session_404(client):
    body = b"{}"
    r = client.post(
        "/sessions/deadbeef/frames",
        content=body,
        headers=_sign("POST", "/sessions/deadbeef/frames", body, "whatever"),
    )
    assert r.status_code == 404
