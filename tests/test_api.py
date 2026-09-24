"""HTTP 协议与 HMAC-SHA256 鉴权测试（真实签名/验签，含防重放与防篡改）。"""

import json
import time

import numpy as np
import pytest

from app.core.ik import IKStatus


@pytest.fixture
def reachable_payload(fk):
    q = np.array([0.5, -0.4, 0.8, 0.3, 0.4, 0.6])
    T = fk(q)
    return {
        "position": [float(x) for x in T[:3, 3]],
        "orientation": {"rotation_matrix": T[:3, :3].tolist()},
        "current_joints": [0.0] * 6,
    }


def test_health(client_no_auth):
    r = client_no_auth.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_robot_info_exposes_dh_and_limits(client_no_auth, robot):
    r = client_no_auth.get("/api/v1/robot/info")
    assert r.status_code == 200
    body = r.json()
    assert len(body["dh"]) == 6
    assert body["joint_limits_rad"]["lower"][0] == pytest.approx(robot.joint_lower[0])
    assert body["auth_required"] is False


def test_ik_success_roundtrip_http(client_no_auth, reachable_payload):
    r = client_no_auth.post("/api/v1/ik", json=reachable_payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["status"] == "SUCCESS"
    assert len(body["joints"]) == 6
    assert body["position_error"] < 1e-4
    assert body["orientation_error"] < 1e-4
    assert len(body["candidates"]) >= 1


def test_ik_quaternion_equals_matrix(client_no_auth, robot):
    """四元数与等价旋转矩阵两种输入应给出同一可达目标的成功解。"""
    # 内部可达腕点
    wc = np.array([0.45, 0.1, 0.55])
    # 绕 (0,0,1) 转 0.6 rad 的四元数 [x,y,z,w]
    angle = 0.6
    quat = [0.0, 0.0, float(np.sin(angle / 2)), float(np.cos(angle / 2))]
    c, s = np.cos(angle), np.sin(angle)
    R = [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]

    def payload(ori):
        return {
            "position": (wc + 0.12 * np.array(ori)[:, 2]).tolist()
            if isinstance(ori, np.ndarray)
            else (wc + 0.12 * np.array(R)[:, 2]).tolist(),
            "orientation": ori,
        }

    rq = client_no_auth.post(
        "/api/v1/ik",
        json={"position": (wc + 0.12 * np.array(R)[:, 2]).tolist(),
              "orientation": {"quaternion": quat}},
    )
    rm = client_no_auth.post(
        "/api/v1/ik",
        json={"position": (wc + 0.12 * np.array(R)[:, 2]).tolist(),
              "orientation": {"rotation_matrix": R}},
    )
    assert rq.status_code == 200, rq.text
    assert rm.status_code == 200, rm.text
    assert rq.json()["success"] and rm.json()["success"]
    np.testing.assert_allclose(rq.json()["joints"], rm.json()["joints"], atol=1e-3)


def test_ik_unreachable_http(client_no_auth):
    body = {"position": [10.0, 0.0, 0.0], "orientation": {"rotation_matrix": np.eye(3).tolist()}}
    r = client_no_auth.post("/api/v1/ik", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is False
    assert data["status"] == "UNREACHABLE"
    assert data["joints"] is None


def test_ik_limit_conflict_http(client_no_auth, fk):
    q = np.array([3.1, -0.2, 0.4, 0.1, 0.2, 0.1])
    T = fk(q)
    body = {"position": T[:3, 3].tolist(), "orientation": {"rotation_matrix": T[:3, :3].tolist()}}
    r = client_no_auth.post("/api/v1/ik", json=body)
    data = r.json()
    assert data["status"] == "LIMIT_CONFLICT"
    assert 1 in data["diagnostics"]["offending_joints"]


def test_bad_rotation_matrix_rejected(client_no_auth):
    R = [[1.0, 0.1, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    body = {"position": [0.5, 0, 0.5], "orientation": {"rotation_matrix": R}}
    r = client_no_auth.post("/api/v1/ik", json=body)
    assert r.status_code == 422


def test_missing_orientation_rejected(client_no_auth):
    r = client_no_auth.post("/api/v1/ik", json={"position": [0.5, 0, 0.5]})
    assert r.status_code == 422


# ---------------- 鉴权 ----------------

KEY = "test-secret-key-0123456789"


def test_auth_required_rejects_unsigned(client_auth, reachable_payload):
    r = client_auth.post("/api/v1/ik", json=reachable_payload)
    assert r.status_code == 401


def test_auth_accepts_valid_signature(client_auth, reachable_payload, make_headers):
    raw = json.dumps(reachable_payload).encode()
    headers = make_headers(KEY, raw)
    r = client_auth.post("/api/v1/ik", content=raw,
                            headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_auth_rejects_wrong_key(client_auth, reachable_payload, make_headers):
    raw = json.dumps(reachable_payload).encode()
    headers = make_headers("wrong-key", raw)
    r = client_auth.post("/api/v1/ik", content=raw,
                            headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 401


def test_auth_rejects_tampered_body(client_auth, reachable_payload, make_headers):
    raw = json.dumps(reachable_payload).encode()
    headers = make_headers(KEY, raw)
    tampered = json.dumps({**reachable_payload, "position": [9.0, 0.0, 0.0]}).encode()
    r = client_auth.post("/api/v1/ik", content=tampered,
                            headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 401


def test_auth_rejects_bad_signature(client_auth, reachable_payload, make_headers):
    raw = json.dumps(reachable_payload).encode()
    headers = make_headers(KEY, raw, tamper=True)
    r = client_auth.post("/api/v1/ik", content=raw,
                            headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 401


def test_auth_rejects_stale_timestamp(client_auth, reachable_payload, make_headers):
    raw = json.dumps(reachable_payload).encode()
    old = int(time.time()) - 10_000
    headers = make_headers(KEY, raw, ts=old)
    r = client_auth.post("/api/v1/ik", content=raw,
                            headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 401


def test_auth_rejects_replayed_nonce(client_auth, reachable_payload, make_headers):
    raw = json.dumps(reachable_payload).encode()
    headers = make_headers(KEY, raw, nonce="fixed-nonce-xyz")
    r1 = client_auth.post("/api/v1/ik", content=raw,
                            headers={**headers, "Content-Type": "application/json"})
    assert r1.status_code == 200
    r2 = client_auth.post("/api/v1/ik", content=raw,
                            headers={**headers, "Content-Type": "application/json"})
    assert r2.status_code == 401


def test_health_remains_open_under_auth(client_auth):
    assert client_auth.get("/healthz").status_code == 200
