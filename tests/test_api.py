"""HTTP 协议层测试（FastAPI TestClient，真实 ASGI 调用，非 mock）。"""

import hashlib
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from groundseg.app import app
from groundseg.crypto import canonical_json, verify_mac
from groundseg import scenes

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_info_lists_defaults_and_labels():
    r = client.get("/api/v1/info")
    assert r.status_code == 200
    body = r.json()
    assert set(body["labels"]) == {"ground", "non_ground", "undecidable"}
    assert body["defaults"]["max_tilt_deg"] == 20.0


def _post(raw_body: bytes):
    return client.post(
        "/api/v1/segment", content=raw_body,
        headers={"content-type": "application/json"},
    )


def test_segment_flat_cloud_with_request_hash():
    pts, _, _ = scenes.flat_ground(size=3.0, spacing=0.3)
    raw = json.dumps({"points": pts.tolist()}).encode("utf-8")
    r = _post(raw)
    assert r.status_code == 200, r.text
    body = r.json()
    # 请求 SHA-256 必须等于客户端对原始字节的独立复算
    assert body["request_sha256"] == hashlib.sha256(raw).hexdigest()
    assert body["request_digest_algorithm"] == "SHA-256"
    assert body["n_points"] == pts.shape[0]
    assert body["stats"]["ground"] == pts.shape[0]
    # 每个点都带回全局 index 与 id
    for i, p in enumerate(body["points"]):
        assert p["global_index"] == i
        assert p["id"] == str(i)
        assert p["label"] == "ground"
    assert "response_sha256" in body


def test_segment_with_custom_ids_preserved():
    pts = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0], [2, 0, 0],
           [0, 2, 0], [2, 1, 0], [1, 2, 0], [2, 2, 0], [3, 3, 0],
           [3, 2, 0], [2, 3, 0], [0, 3, 0], [3, 0, 0]] * 3
    ids = [f"pt-{i}" for i in range(len(pts))]
    r = client.post("/api/v1/segment", json={"points": pts, "point_ids": ids})
    assert r.status_code == 200
    out_ids = [p["id"] for p in r.json()["points"]]
    assert out_ids == ids


def test_segment_steep_slope_returns_undecidable():
    pts, _, _ = scenes.SCENES["steep_slope"]()
    r = client.post("/api/v1/segment", json={"points": pts.tolist()})
    body = r.json()
    assert body["stats"]["ground"] == 0
    assert body["stats"]["undecidable"] == pts.shape[0]


def test_segment_params_override_takes_effect():
    pts, _, _ = scenes.SCENES["steep_slope"]()
    # 默认参数：拒判
    r1 = client.post("/api/v1/segment", json={"points": pts.tolist()})
    assert r1.json()["stats"]["ground"] == 0
    # 放宽倾角：分出地面
    r2 = client.post("/api/v1/segment", json={
        "points": pts.tolist(),
        "params": {"max_tilt_deg": 40.0, "min_inlier_count": 10},
    })
    assert r2.status_code == 200
    assert r2.json()["params_used"]["max_tilt_deg"] == 40.0
    assert r2.json()["stats"]["ground"] > 0


def test_segment_with_seed_indices():
    pts, _, _ = scenes.flat_ground(size=3.0, spacing=0.3)
    r = client.post("/api/v1/segment", json={
        "points": pts.tolist(), "seed_indices": [0, 1, 2, 10],
    })
    assert r.status_code == 200
    assert r.json()["stats"]["ground"] == pts.shape[0]


def test_segment_rejects_bad_shape():
    r = client.post("/api/v1/segment", json={"points": [[1, 2], [3, 4]]})
    assert r.status_code == 422


def test_segment_rejects_non_finite():
    # 用原始字节发送 NaN（Python/httpx 的 json= 编码器本身会拒绝 NaN）
    raw = b'{"points": [[1.0, 2.0, NaN]]}'
    r = client.post("/api/v1/segment", content=raw,
                    headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_segment_rejects_duplicate_ids():
    r = client.post("/api/v1/segment", json={
        "points": [[0, 0, 0], [1, 0, 0]], "point_ids": ["a", "a"],
    })
    assert r.status_code == 422


def test_segment_rejects_seed_out_of_range():
    r = client.post("/api/v1/segment", json={
        "points": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "seed_indices": [5],
    })
    assert r.status_code == 422


def test_segment_rejects_invalid_params():
    r = client.post("/api/v1/segment", json={
        "points": [[0, 0, 0]], "params": {"max_tilt_deg": 120},
    })
    assert r.status_code == 422


def test_empty_cloud():
    r = client.post("/api/v1/segment", json={"points": []})
    assert r.status_code == 200
    assert r.json()["stats"]["total"] == 0


def test_evaluate_endpoint_metrics():
    r = client.post("/api/v1/evaluate", json={
        "predicted": ["ground", "non_ground", "undecidable", "ground"],
        "truth": [1, 0, 1, 0],
    })
    assert r.status_code == 200
    g = r.json()["metrics"]["ground"]
    assert g["precision"] == 0.5
    assert g["recall"] == 0.5
    assert g["confusion"]["undecided"] == 1
    assert "request_sha256" in r.json()


def test_evaluate_endpoint_length_mismatch():
    r = client.post("/api/v1/evaluate", json={
        "predicted": ["ground"], "truth": [1, 0],
    })
    assert r.status_code == 422


def test_evaluate_endpoint_bad_label():
    r = client.post("/api/v1/evaluate", json={
        "predicted": ["sky"], "truth": [1],
    })
    assert r.status_code == 422


def test_hmac_signature_present_and_verifiable(monkeypatch):
    monkeypatch.setenv("GROUNDSEG_HMAC_KEY", "unit-test-key")
    pts = [[0, 0, 0], [1, 0, 0], [0, 1, 0]] * 6
    r = client.post("/api/v1/segment", json={"points": pts})
    body = r.json()
    assert "signature" in body
    sig = body["signature"]
    payload = {k: v for k, v in body.items() if k != "signature"}
    assert sig["algorithm"] == "HMAC-SHA256"
    assert verify_mac(payload, sig["mac"], b"unit-test-key")
    # 篡改响应体后验签必须失败
    payload["n_points"] += 1
    assert not verify_mac(payload, sig["mac"], b"unit-test-key")


def test_response_sha256_covers_full_body():
    pts, _, _ = scenes.flat_ground(size=2.0, spacing=0.5)
    raw = json.dumps({"points": pts.tolist()}).encode()
    body = _post(raw).json()
    claimed = body.pop("response_sha256")
    assert claimed == hashlib.sha256(canonical_json(body)).hexdigest()


def test_request_sha256_detects_different_payloads():
    r1 = _post(json.dumps({"points": [[0, 0, 0]] * 40}).encode())
    r2 = _post(json.dumps({"points": [[1, 1, 1]] * 40}).encode())
    assert r1.json()["request_sha256"] != r2.json()["request_sha256"]
