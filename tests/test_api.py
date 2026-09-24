"""FastAPI 端到端测试：通过 TestClient 发真实 HTTP/ASGI 请求。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.crypto import canonical_json
from app.main import app, store as _store


@pytest.fixture()
def client():
    # 每个测试使用全新内存存储
    _store._sessions.clear()
    with TestClient(app) as c:
        yield c
    _store._sessions.clear()


def create(client, sid="s1", **config):
    body = {"session_id": sid}
    if config:
        body["config"] = config
    resp = client.post("/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def frame(client, sid, frame_id, ts, dets):
    return client.post(
        f"/sessions/{sid}/frames",
        json={"frame_id": frame_id, "timestamp": ts, "detections": dets},
    )


# ------------------------------------------------------------------ 基础流程
def test_health_and_session_lifecycle(client):
    assert client.get("/health").json()["status"] == "ok"
    s = create(client, "life")
    assert s["signing_key_hex"] and len(s["signing_key_hex"]) == 64
    got = client.get("/sessions/life")
    assert got.status_code == 200
    assert got.json()["signing_key_hex"] is None  # 密钥只返回一次
    assert client.delete("/sessions/life").status_code == 204
    assert client.get("/sessions/life").status_code == 404


def test_duplicate_session_id_conflicts(client):
    create(client, "dup")
    resp = client.post("/sessions", json={"session_id": "dup"})
    assert resp.status_code == 409


def test_frame_roundtrip_and_signature(client):
    s = create(client, "sig")
    resp = frame(client, "sig", 0, 0.0, [{"x": 1.0, "y": 2.0, "detection_id": "a"}])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["frame_id"] == 0
    assert body["dt"] is None
    assert body["tracks"][0]["position"] == [1.0, 2.0]
    # 真实 HMAC 校验
    import hashlib
    import hmac

    sig = body["signature"]
    unsigned = {k: v for k, v in body.items() if k != "signature"}
    expected = hmac.new(
        bytes.fromhex(s["signing_key_hex"]),
        canonical_json(unsigned).encode(),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(sig, expected)


def test_second_frame_dt_is_real(client):
    create(client, "dt")
    frame(client, "dt", 0, 0.0, [{"x": 0.0, "y": 0.0}])
    r = frame(client, "dt", 1, 2.5, [{"x": 1.0, "y": 0.0}]).json()
    assert r["dt"] == 2.5


# ------------------------------------------------------------- 幂等：不增 ID
def test_replayed_frame_is_idempotent(client):
    create(client, "idem")
    payload = {"frame_id": 0, "timestamp": 0.0,
               "detections": [{"x": 0.0, "y": 0.0}]}
    first = client.post("/sessions/idem/frames", json=payload).json()
    assert first["replay"] is False
    assert first["next_track_id"] == 2

    replay = client.post("/sessions/idem/frames", json=payload).json()
    assert replay["replay"] is True
    assert replay["next_track_id"] == 2  # ID 不增长
    assert replay["births"] == [] or replay["births"] == first["births"]
    # 再推进一帧，ID 仍接着 1 之后分配，没有重复消耗
    nxt = frame(client, "idem", 1, 1.0,
                [{"x": 9.0, "y": 9.0}, {"x": 0.0, "y": 0.0}]).json()
    assert nxt["births"] == [2]


def test_same_frame_id_different_payload_conflicts(client):
    create(client, "conf")
    frame(client, "conf", 0, 0.0, [{"x": 0.0, "y": 0.0}])
    resp = frame(client, "conf", 0, 0.0, [{"x": 3.0, "y": 3.0}])
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "frame_payload_conflict"


# ----------------------------------------------------------------- 乱序拒绝
def test_stale_frame_id_and_timestamp_rejected(client):
    create(client, "stale")
    frame(client, "stale", 0, 0.0, [])
    frame(client, "stale", 1, 1.0, [])

    # 回退 frame_id
    r = client.post(
        "/sessions/stale/frames",
        json={"frame_id": 0, "timestamp": 2.0, "detections": []},
    )
    # 注意：frame_id=0 已有缓存且负载不同（空帧负载相同？第一帧也是空帧）
    # 第一帧负载 == {frame_id:0,timestamp:0.0,dets:[]}，这里 timestamp 不同 => 冲突 409
    assert r.status_code in (409, 422)

    # frame_id 前进但 timestamp 回退 -> 422
    r2 = client.post(
        "/sessions/stale/frames",
        json={"frame_id": 2, "timestamp": 0.5, "detections": []},
    )
    assert r2.status_code == 422
    assert r2.json()["detail"]["error"] == "stale_frame"


def test_replayed_empty_frame_returns_replay(client):
    create(client, "e")
    payload = {"frame_id": 0, "timestamp": 0.0, "detections": []}
    client.post("/sessions/e/frames", json=payload)
    again = client.post("/sessions/e/frames", json=payload)
    assert again.status_code == 200
    assert again.json()["replay"] is True


# -------------------------------------------------------------- 输入校验
def test_nan_and_inf_rejected(client):
    create(client, "nan")
    r = client.post(
        "/sessions/nan/frames",
        json={"frame_id": 0, "timestamp": 0.0,
              "detections": [{"x": "NaN", "y": 0.0}]},
    )
    assert r.status_code == 422


def test_extra_field_rejected(client):
    create(client, "x")
    r = client.post(
        "/sessions/x/frames",
        json={"frame_id": 0, "timestamp": 0.0, "detections": [], "evil": 1},
    )
    assert r.status_code == 422


def test_unknown_session_404(client):
    assert frame(client, "ghost", 0, 0.0, []).status_code == 404


# ----------------------------------------------------------- 完整跟踪小场景
def test_full_crossing_scenario_over_http(client):
    create(client, "cross")
    for k in range(15):
        r = frame(
            client,
            "cross",
            k,
            float(k),
            [
                {"x": float(k), "y": float(k), "detection_id": f"A-{k}"},
                {"x": float(8 + k), "y": float(12 - k), "detection_id": f"B-{k}"},
            ],
        )
        assert r.status_code == 200
    final = r.json()
    ids = sorted(t["track_id"] for t in final["tracks"])
    assert ids == [1, 2]
    assert all(t["state"] == "confirmed" for t in final["tracks"])
