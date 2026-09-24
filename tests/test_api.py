"""HTTP 层测试：协议、错误信封、单位校验、真实 HMAC 签名验签。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    # 每个测试独立控制密钥环境变量
    monkeypatch.delenv("IMU_BIAS_HMAC_SECRET", raising=False)
    monkeypatch.delenv("IMU_BIAS_HMAC_SECRET_FILE", raising=False)
    from app.main import app

    return TestClient(app)


def _payload():
    fs = 100.0
    t = np.arange(0, 12, 1 / fs)
    g = np.tile([0.01, -0.02, 0.005], (len(t), 1))
    a = np.zeros((len(t), 3))
    a[:, 2] = 9.80665
    return {
        "timestamps": t.tolist(),
        "accel": a.tolist(),
        "gyro": g.tolist(),
    }


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_ready_shows_auth_state(client):
    r = client.get("/ready")
    assert r.json()["hmac_auth_enabled"] is False


def test_estimate_success(client):
    r = client.post("/estimate", json=_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["gyroscope_bias"]["status"] == "estimated"
    np.testing.assert_allclose(
        body["gyroscope_bias"]["bias_at_reference_rad_s"],
        [0.01, -0.02, 0.005],
        atol=2e-3,
    )


def test_length_mismatch_422(client):
    p = _payload()
    p["gyro"] = p["gyro"][:-1]
    r = client.post("/estimate", json=p)
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]["issues"]


def test_bad_json_400(client):
    r = client.post("/estimate", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_nonmonotonic_timestamp_422(client):
    p = _payload()
    ts = p["timestamps"]
    ts[10], ts[11] = ts[11], ts[10]
    r = client.post("/estimate", json=p)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "preprocess_error"


def test_bad_units_422(client):
    p = _payload()
    p["units"] = {"accel": "knots", "gyro": "rads", "time": "s"}
    r = client.post("/estimate", json=p)
    assert r.status_code == 422


def test_bad_axes_422(client):
    p = _payload()
    p["axes"] = {"x": "+x", "y": "+x", "z": "+z"}  # x 被引用两次
    r = client.post("/estimate", json=p)
    assert r.status_code == 422


def test_nan_in_input_returns_valid_json_422(client):
    """NaN 输入必须被拒绝，且错误信封本身仍是合法、严格的 JSON（回归测试）。"""
    raw = (
        b'{"timestamps":[1,2],"accel":[[0,0,9.8],[0,0,NaN]],'
        b'"gyro":[[0,0,0],[0,0,0]]}'
    )
    r = client.post(
        "/estimate", content=raw, headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 422
    # 标准库 json 默认 parse_constant 会把 NaN/Infinity 解析成非法浮点令牌；
    # 严格解析器遇到裸 NaN 字面量会抛错
    body = json.loads(
        r.content, parse_constant=lambda val: pytest.fail(f"非法 JSON 常量: {val}")
    )
    assert body["ok"] is False
    assert body["error"]["code"] == "validation_error"


# ---------- HMAC 真实密码学验证 ----------

SECRET = b"test-secret-key-0123456789"


def _sign(timestamp: str, method: str, path: str, body: bytes, secret: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    msg = f"{timestamp}.{method}.{path}.{body_hash}".encode()
    return "sha256=" + hmac.new(secret, msg, hashlib.sha256).hexdigest()


@pytest.fixture
def auth_client(monkeypatch):
    monkeypatch.setenv("IMU_BIAS_HMAC_SECRET", SECRET.decode())
    from app.main import app

    return TestClient(app)


def test_hmac_enabled_requires_signature(auth_client):
    r = auth_client.post("/estimate", json=_payload())
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_hmac_valid_signature_passes(auth_client):
    body = json.dumps(_payload()).encode()
    ts = f"{time.time():.3f}"
    sig = _sign(ts, "POST", "/estimate", body, SECRET)
    r = auth_client.post(
        "/estimate",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": sig,
        },
    )
    assert r.status_code == 200, r.text
    # 响应必须携带可验证的响应签名
    resp_sig = r.headers["X-Response-Signature"]
    expected = "sha256=" + hmac.new(SECRET, r.content, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(resp_sig, expected)


def test_hmac_tampered_body_rejected(auth_client):
    body = json.dumps(_payload()).encode()
    ts = f"{time.time():.3f}"
    sig = _sign(ts, "POST", "/estimate", body, SECRET)
    tampered = body.replace(b"0.01", b"9.99", 1)
    r = auth_client.post(
        "/estimate",
        content=tampered,
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": sig,
        },
    )
    assert r.status_code == 401


def test_hmac_wrong_secret_rejected(monkeypatch):
    monkeypatch.setenv("IMU_BIAS_HMAC_SECRET", SECRET.decode())
    from app.main import app

    with TestClient(app) as c:
        body = json.dumps(_payload()).encode()
        ts = f"{time.time():.3f}"
        sig = _sign(ts, "POST", "/estimate", body, b"a-different-secret")
        r = c.post(
            "/estimate",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Timestamp": ts,
                "X-Signature": sig,
            },
        )
        assert r.status_code == 401


def test_hmac_replay_old_timestamp_rejected(auth_client):
    body = json.dumps(_payload()).encode()
    ts = f"{time.time() - 1000:.3f}"
    sig = _sign(ts, "POST", "/estimate", body, SECRET)
    r = auth_client.post(
        "/estimate",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": sig,
        },
    )
    assert r.status_code == 401
    assert "重放" in r.json()["error"]["message"]


def test_hmac_secret_file(monkeypatch, tmp_path):
    keyfile = tmp_path / "secret.txt"
    keyfile.write_bytes(SECRET)
    monkeypatch.setenv("IMU_BIAS_HMAC_SECRET_FILE", str(keyfile))
    from app.main import app

    with TestClient(app) as c:
        assert c.get("/ready").json()["hmac_auth_enabled"] is True
        body = json.dumps(_payload()).encode()
        ts = f"{time.time():.3f}"
        sig = _sign(ts, "POST", "/estimate", body, SECRET)
        r = c.post(
            "/estimate",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Timestamp": ts,
                "X-Signature": sig,
            },
        )
        assert r.status_code == 200
