"""密码操作必须真实执行：HMAC 签名、时间窗、nonce 防重放、响应签名。"""
from __future__ import annotations

import json
import time

from app import config, crypto


def test_hmac_vector_is_real():
    # RFC 2202 风格自测：固定输入得到确定的 64 位十六进制摘要
    sig = crypto.sign_text("key", "The quick brown fox jumps over the dog")
    assert len(sig) == 64
    assert sig == crypto.sign_text("key",
                                   "The quick brown fox jumps over the dog")
    assert sig != crypto.sign_text("other-key",
                                   "The quick brown fox jumps over the dog")


def test_sign_and_verify_roundtrip():
    body = b'{"id":"R1"}'
    headers = crypto.sign_request("POST", "/api/robots", body)
    msg = crypto.signing_string(
        headers["X-Key-Id"], headers["X-Timestamp"], headers["X-Nonce"],
        "POST", "/api/robots", body,
    )
    assert crypto.verify_signature(config.API_SECRET, msg,
                                   headers["X-Signature"])
    # 篡改一个字节即验签失败
    assert not crypto.verify_signature(config.API_SECRET, msg + "x",
                                       headers["X-Signature"])


def test_body_tampering_invalidates_signature(api):
    body = json.dumps({"id": "R1", "x": 0, "y": 0,
                       "battery_wh": 10}, separators=(",", ":")).encode()
    h = crypto.sign_request("POST", "/api/robots", body)
    tampered = body.replace(b"10", b"99")
    r = api.post("/api/robots", raw_body=tampered, extra_headers=h)
    assert r.status_code == 401


def test_missing_signature_headers_rejected(api, client):
    r = client.post("/api/robots", json={"id": "R1", "x": 0, "y": 0,
                                         "battery_wh": 10})
    assert r.status_code == 401


def test_wrong_secret_rejected(api):
    body = json.dumps({"id": "R1", "x": 0, "y": 0,
                       "battery_wh": 10}, separators=(",", ":")).encode()
    h = crypto.sign_request("POST", "/api/robots", body,
                            secret="totally-wrong-secret")
    r = api.post("/api/robots", raw_body=body, extra_headers=h)
    assert r.status_code == 401


def test_stale_timestamp_rejected(api):
    body = b"{}"
    h = crypto.sign_request("GET", "/api/snapshot", body,
                            timestamp=str(int(time.time()) - 9999))
    r = api.get("/api/snapshot", raw_body=body, extra_headers=h)
    assert r.status_code == 401
    assert "window" in r.json()["detail"]


def test_nonce_replay_rejected(api):
    body = json.dumps({"id": "R1", "x": 0, "y": 0,
                       "battery_wh": 10}, separators=(",", ":")).encode()
    h = crypto.sign_request("POST", "/api/robots", body)
    r1 = api.post("/api/robots", raw_body=body, extra_headers=dict(h))
    assert r1.status_code == 200
    # 完全相同的头与体重放 -> 拒绝
    r2 = api.post("/api/robots", raw_body=body, extra_headers=dict(h))
    assert r2.status_code == 401
    assert "replay" in r2.json()["detail"]


def test_response_is_signed(api, client):
    body = json.dumps({"id": "R1", "x": 0, "y": 0,
                       "battery_wh": 10}, separators=(",", ":")).encode()
    h = crypto.sign_request("POST", "/api/robots", body)
    r = client.post("/api/robots", content=body, headers={
        **h, "content-type": "application/json"})
    assert r.status_code == 200
    assert r.headers["X-Response-Algorithm"] == "HMAC-SHA256"
    assert crypto.verify_signature(
        config.API_SECRET, r.content.decode(), r.headers["X-Response-Signature"]
    )


def test_healthz_is_open(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
