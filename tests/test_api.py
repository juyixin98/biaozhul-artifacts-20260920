"""HTTP-level tests: protocol, status codes, integrity hashing and HMAC."""

from __future__ import annotations

import hashlib
import hmac
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.geometry import quat_to_rot
from app.integrity import canonical_json
from app.main import app
from tests._helpers import pose, q_from_axis_angle, transform_pose

client = TestClient(app)


def _rect_gt():
    pts = np.array(
        [[0, 0, 0], [1, 0, 0], [2, 0, 0], [2, 1, 0], [1, 1, 0], [0, 1, 0]], dtype=float
    )
    yaw = np.deg2rad([0, 0, 90, 90, 180, 180])
    qs = [q_from_axis_angle(np.array([0.0, 0.0, 1.0]), a) for a in yaw]
    return [pose(0.1 * i, p, q) for i, (p, q) in enumerate(zip(pts, qs, strict=True))]


def _good_body(mode="rigid"):
    gt = _rect_gt()
    R = quat_to_rot(q_from_axis_angle(np.array([0.0, 0.0, 1.0]), 0.5))
    est = [
        pose(p["time"] + 0.003, *transform_pose(p["position"], p["quaternion_xyzw"], R, [1, 2, 0]))
        for p in gt
    ]
    return {
        "estimated": est,
        "ground_truth": gt,
        "association": {"max_time_diff": 0.02},
        "alignment": {"mode": mode},
        "rpe": {"delta_index": 1},
    }


def test_healthz_hash_is_real_sha256():
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    payload = {k: v for k, v in body.items() if k != "integrity"}
    assert body["integrity"]["response_sha256"] == hashlib.sha256(
        canonical_json(payload)
    ).hexdigest()


def test_evaluate_returns_consistent_integrity():
    body = _good_body()
    raw = json.dumps(body).encode()
    r = client.post("/api/v1/evaluate", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 200, r.text
    resp = r.json()
    assert resp["integrity"]["request_sha256"] == hashlib.sha256(raw).hexdigest()
    payload = {k: v for k, v in resp.items() if k != "integrity"}
    assert resp["integrity"]["response_sha256"] == hashlib.sha256(
        canonical_json(payload)
    ).hexdigest()
    assert "hmac_sha256" not in resp["integrity"]
    assert resp["match"]["coverage"] == 1.0


def test_hmac_present_when_key_configured(monkeypatch):
    monkeypatch.setenv("TRAJECTORY_EVAL_HMAC_KEY", "secret-key")
    body = _good_body()
    r = client.post("/api/v1/evaluate", json=body)
    assert r.status_code == 200
    resp = r.json()
    payload = {k: v for k, v in resp.items() if k != "integrity"}
    expected = hmac.new(b"secret-key", canonical_json(payload), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(resp["integrity"]["hmac_sha256"], expected)


def test_duplicate_timestamp_returns_422_structured_error():
    body = _good_body()
    body["ground_truth"][2]["time"] = body["ground_truth"][1]["time"]
    r = client.post("/api/v1/evaluate", json=body)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "DUPLICATE_TIMESTAMP"
    assert "timestamp" in err["details"]


def test_no_matches_returns_422():
    body = _good_body()
    for p in body["estimated"]:
        p["time"] += 100.0
    r = client.post("/api/v1/evaluate", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NO_MATCHES"


def test_degenerate_alignment_returns_422():
    body = _good_body()
    # Collapse GT to a straight line -> rank 1, alignment undetermined.
    for i, p in enumerate(body["ground_truth"]):
        p["position"] = [float(i), 0.0, 0.0]
        p["quaternion_xyzw"] = [0.0, 0.0, 0.0, 1.0]
    for i, p in enumerate(body["estimated"]):
        p["position"] = [float(i), 0.0, 1.0]
        p["quaternion_xyzw"] = [0.0, 0.0, 0.0, 1.0]
    r = client.post("/api/v1/evaluate", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ALIGNMENT_DEGENERATE"


def test_invalid_quaternion_returns_422():
    body = _good_body()
    body["estimated"][0]["quaternion_xyzw"] = [0.0, 0.0, 0.0, 0.0]
    r = client.post("/api/v1/evaluate", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_POSE"


def test_bad_json_and_schema_errors():
    r = client.post("/api/v1/evaluate", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_JSON"

    r = client.post("/api/v1/evaluate", json={"estimated": []})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"

    r = client.post("/api/v1/evaluate", json={"estimated": [], "bogus": 1})
    assert r.status_code == 422


def test_scale_mode_flag_is_distinguished():
    rigid = client.post("/api/v1/evaluate", json=_good_body("rigid")).json()
    sim = client.post("/api/v1/evaluate", json=_good_body("similarity")).json()
    assert rigid["scale_alignment_applied"] is False
    assert sim["scale_alignment_applied"] is True
    assert rigid["alignment"]["scale"] == 1.0
    assert abs(sim["alignment"]["scale"] - 1.0) < 1e-9  # isometric data -> s ~ 1
