"""演示 HTTP 服务测试：业务函数直测 + 真实 socket 集成测试。"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request

import pytest

from envelope_enc.container import IntegrityError, decrypt_bytes
from envelope_enc.keyring import Keyring
from envelope_enc.server import ApiState, handle_api, make_server


@pytest.fixture()
def state(tmp_path):
    kr = Keyring(str(tmp_path / "keys.json"))
    kr.initialize()
    return ApiState(keyring=kr, token="test-token")


# --------------------------------------------------------- 业务函数级测试
def test_api_health(state):
    status, body = handle_api("GET", "/health", {}, state)
    assert status == 200 and body["status"] == "ok"


def test_api_encrypt_decrypt_roundtrip(state):
    payload = b"http payload " * 100
    _, enc_body = handle_api(
        "POST", "/encrypt", {"data_b64": base64.b64encode(payload).decode()}, state
    )
    kid_v1 = enc_body["kid"]
    assert enc_body["file_id"].startswith("f-")

    # 服务端主密钥轮换后再走 /rotate
    handle_api("POST", "/keys/rotate-master", {}, state)
    _, rot_body = handle_api(
        "POST", "/rotate",
        {"ciphertext_b64": enc_body["ciphertext_b64"]}, state,
    )
    assert rot_body["old_kid"] == kid_v1
    assert rot_body["new_kid"] != kid_v1
    assert rot_body["skipped"] is False

    _, dec_body = handle_api(
        "POST", "/decrypt",
        {"ciphertext_b64": rot_body["ciphertext_b64"]}, state,
    )
    assert base64.b64decode(dec_body["data_b64"]) == payload


def test_api_rotate_skipped_is_idempotent(state):
    _, enc = handle_api(
        "POST", "/encrypt", {"data_b64": base64.b64encode(b"x").decode()}, state
    )
    _, r1 = handle_api("POST", "/rotate", {"ciphertext_b64": enc["ciphertext_b64"]}, state)
    assert r1["skipped"] is True
    assert r1["old_kid"] == r1["new_kid"]


def test_api_decrypt_tampered_returns_400(state):
    _, enc = handle_api(
        "POST", "/encrypt",
        {"data_b64": base64.b64encode(b"x" * 300).decode(), "chunk_size": 32},
        state,
    )
    blob = bytearray(base64.b64decode(enc["ciphertext_b64"]))
    blob[-1] ^= 0x01
    with pytest.raises(IntegrityError):
        handle_api(
            "POST", "/decrypt",
            {"ciphertext_b64": base64.b64encode(bytes(blob)).decode()},
            state,
        )


def test_api_bad_base64_and_missing_field(state):
    from envelope_enc.server import ApiError

    with pytest.raises(ApiError) as e1:
        handle_api("POST", "/encrypt", {"data_b64": "!!!not-b64"}, state)
    assert e1.value.status == 400
    with pytest.raises(ApiError) as e2:
        handle_api("POST", "/encrypt", {}, state)
    assert e2.value.status == 400


def test_api_keys_listing(state):
    _, body = handle_api("GET", "/keys", {}, state)
    assert len(body["keys"]) == 1 and body["keys"][0]["status"] == "active"
    handle_api("POST", "/keys/rotate-master", {}, state)
    _, body = handle_api("GET", "/keys", {}, state)
    statuses = sorted(k["status"] for k in body["keys"])
    assert statuses == ["active", "retired"]


# --------------------------------------------------------- 真实 HTTP 集成
@pytest.fixture()
def live_server(tmp_path):
    kr = Keyring(str(tmp_path / "keys.json"))
    kr.initialize()
    httpd = make_server("127.0.0.1", 0, kr, "integration-token")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(url, method="GET", payload=None, token="integration-token"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_http_roundtrip_and_unauthorized(live_server):
    # 健康检查无需 token
    status, body = _request(f"{live_server}/health", token=None)
    assert status == 200

    # 无 token 被拒
    status, body = _request(f"{live_server}/keys", token=None)
    assert status == 401 and "未授权" in body["error"]

    # 错误 token 被拒
    status, _ = _request(f"{live_server}/keys", token="wrong")
    assert status == 401

    # 正常流程
    status, enc = _request(
        live_server + "/encrypt", "POST",
        {"data_b64": base64.b64encode(b"socket-test" * 20).decode()},
    )
    assert status == 200
    status, dec = _request(
        live_server + "/decrypt", "POST",
        {"ciphertext_b64": enc["ciphertext_b64"]},
    )
    assert status == 200
    assert base64.b64decode(dec["data_b64"]) == b"socket-test" * 20


def test_http_unknown_route_404(live_server):
    status, body = _request(live_server + "/nope", "POST", {})
    assert status == 404
