"""FastAPI + 真实 HMAC-SHA256 密码学端到端测试。"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from app.config import settings


def _post(client, path, payload, *, headers=None, raw_override=None):
    raw = (
        raw_override
        if raw_override is not None
        else json.dumps(payload, separators=(",", ":")).encode()
    )
    return client.post(path, content=raw, headers=headers or {"Content-Type": "application/json"})


def test_health_and_state_open(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/state").json()["initialized"] is False


def test_post_without_signature_is_401(client):
    r = _post(
        client,
        "/fuse",
        {"id": "x", "type": "gnss", "time": 0.0,
         "measurement": [0, 0], "R": [[1, 0], [0, 1]]},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "missing signature headers"


def test_valid_signature_accepted(client, signed):
    msg = {"id": "g0", "type": "gnss", "time": 100.0,
           "measurement": [1.0, 2.0], "R": [[0.5, 0], [0, 0.5]]}
    raw, headers = signed("POST", "/fuse", msg)
    r = _post(client, "/fuse", msg, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is True
    assert body["step"]["innovation"] == [0.0, 0.0]


def test_tampered_body_fails_signature(client, signed):
    msg = {"id": "g0", "type": "gnss", "time": 100.0,
           "measurement": [1.0, 2.0], "R": [[0.5, 0], [0, 0.5]]}
    _, headers = signed("POST", "/fuse", msg)
    tampered = dict(msg, measurement=[9.0, 9.0])
    r = _post(client, "/fuse", tampered, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "signature mismatch"


def test_wrong_secret_fails(client, signed):
    msg = {"id": "g0", "type": "gnss", "time": 100.0,
           "measurement": [1.0, 2.0], "R": [[0.5, 0], [0, 0.5]]}
    _, headers = signed("POST", "/fuse", msg, secret="attacker-secret")
    r = _post(client, "/fuse", msg, headers=headers)
    assert r.status_code == 401


def test_stale_timestamp_fails(client, signed):
    msg = {"id": "g0", "type": "gnss", "time": 100.0,
           "measurement": [1.0, 2.0], "R": [[0.5, 0], [0, 0.5]]}
    old = f"{time.time() - settings.SIGN_FRESHNESS_S - 5:.6f}"
    _, headers = signed("POST", "/fuse", msg, timestamp=old)
    r = _post(client, "/fuse", msg, headers=headers)
    assert r.status_code == 401
    assert "timestamp" in r.json()["detail"]["reason"]


def test_signature_is_real_hmac_sha256(client):
    """直接用 hmac/hashlib 手工计算签名，验证与服务端一致。"""
    msg = {"id": "g1", "type": "gnss", "time": 200.0,
           "measurement": [0.0, 0.0], "R": [[1.0, 0], [0, 1.0]]}
    raw = json.dumps(msg, separators=(",", ":")).encode()
    ts = f"{time.time():.6f}"  # 签名时间戳是墙钟，与测量时间相互独立
    canonical = f"POST\n/fuse\n{ts}\n{hashlib.sha256(raw).hexdigest()}".encode()
    sig = hmac.new(b"test-secret", canonical, hashlib.sha256).hexdigest()
    r = client.post(
        "/fuse",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": sig,
        },
    )
    assert r.status_code == 200, r.text


def test_bad_covariance_returns_422_with_evidence(client, signed):
    msg = {"id": "bad", "type": "gnss", "time": 100.0,
           "measurement": [1.0, 2.0], "R": [[1.0, 0.9], [0.9, -1.0]]}
    _, headers = signed("POST", "/fuse", msg)
    r = _post(client, "/fuse", msg, headers=headers)
    assert r.status_code == 422
    body = r.json()
    assert body["reason"] == "BAD_COVARIANCE"
    assert body["evidence"]["detail"]["min_eigenvalue"] < 0
    rejections = client.get("/rejections").json()["rejections"]
    assert any(x["reason"] == "BAD_COVARIANCE" for x in rejections)


def test_outlier_returns_200_and_gate_evidence(client, signed):
    m0 = {"id": "o0", "type": "odometry", "time": 100.0,
          "measurement": [1.0, 0.0], "R": [[0.01, 0], [0, 0.01]]}
    _, h0 = signed("POST", "/fuse", m0)
    _post(client, "/fuse", m0, headers=h0)
    bad = {"id": "gz", "type": "gnss", "time": 100.5,
           "measurement": [80.0, 80.0], "R": [[0.25, 0], [0, 0.25]]}
    _, hb = signed("POST", "/fuse", bad)
    r = _post(client, "/fuse", bad, headers=hb)
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"] == "OUTLIER_GATE"
    assert body["step"]["nis"] > 9.2103

    trace = client.get("/trace").json()["steps"]
    gated = [s for s in trace if s["id"] == "gz"][0]
    assert gated["gated"] is True
    assert "innovation" in gated and "S" in gated and "K" in gated


def test_late_out_of_horizon_422_path_is_200_rejected(client, signed):
    # 窗口外拒绝在引擎层是 rejected，HTTP 仍 200（测量本身协议合法）
    for i in range(25):
        m = {"id": f"o{i}", "type": "odometry", "time": 100 + i * 0.1,
             "measurement": [1.0, 0.0], "R": [[0.01, 0], [0, 0.01]]}
        _, h = signed("POST", "/fuse", m)
        _post(client, "/fuse", m, headers=h)
    late = {"id": "late", "type": "gnss", "time": 100.0,
            "measurement": [0.0, 0.0], "R": [[0.5, 0], [0, 0.5]]}
    _, h = signed("POST", "/fuse", late)
    r = _post(client, "/fuse", late, headers=h)
    assert r.status_code == 200
    assert r.json()["reason"] == "LATE_OUT_OF_HORIZON"
    assert r.json()["evidence"]["detail"]["late_by_s"] > 2.0


def test_batch_endpoint_replays_and_reports(client, signed):
    msgs = [
        {"id": "g0", "type": "gnss", "time": 0.2,
         "measurement": [0.2, 0.0], "R": [[0.5, 0], [0, 0.5]]},
        {"id": "o0", "type": "odometry", "time": 0.0,
         "measurement": [1.0, 0.0], "R": [[0.01, 0], [0, 0.01]]},
        {"id": "o1", "type": "odometry", "time": 0.1,
         "measurement": [1.0, 0.0], "R": [[0.01, 0], [0, 0.01]]},
    ]
    payload = {"measurements": msgs}
    _, headers = signed("POST", "/fuse/batch", payload)
    r = client.post("/fuse/batch", content=json.dumps(payload, separators=(",", ":")),
                    headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["received"] == 3 and body["accepted"] == 3
    # 乱序批量：第一条到达的 gnss(0.2) 之后，o0(0.0) 触发了重新初始化重放
    g0 = [x for x in body["results"] if x["id"] == "g0"][0]
    assert g0["status"] == "initialized"
    o0 = [x for x in body["results"] if x["id"] == "o0"][0]
    assert o0["replayed"] >= 1
    trace_times = [s["time"] for s in client.get("/trace").json()["steps"]]
    assert trace_times == sorted(trace_times)


def test_reset_requires_signature(client, signed):
    assert client.post("/reset").status_code == 401
    _, headers = signed("POST", "/reset", {})
    r = client.post("/reset", content=b"{}", headers=headers)
    assert r.status_code == 200
    assert client.get("/state").json()["initialized"] is False
