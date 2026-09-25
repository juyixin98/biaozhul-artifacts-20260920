"""HTTP API 端到端测试（标准库 urllib，真实监听 127.0.0.1 随机端口）。"""

from __future__ import annotations

import base64
import contextlib
import json
import threading
import urllib.error
import urllib.request

import pytest

from keyversion.server import build_server
from keyversion.service import KeyService
from keyversion.store import KeyStore


@pytest.fixture
def http_service(tmp_path):
    store = KeyStore(tmp_path / "ks").open()
    svc = KeyService(store)
    server = build_server("127.0.0.1", 0, svc)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        yield base, svc
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        store.close()


def _request(base, method, path, body=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_health_and_empty_keys(http_service):
    base, _ = http_service
    status, body = _request(base, "GET", "/health")
    assert status == 200 and body["status"] == "ok"
    status, body = _request(base, "GET", "/keys")
    assert status == 200 and body["versions"] == []


def test_full_lifecycle_and_rotation_via_http(http_service):
    base, _ = http_service
    # 无 active 时加密被拒（409）
    status, body = _request(base, "POST", "/encrypt", {"plaintext": "hello"})
    assert status == 409 and body["error"]["code"] == "no_active_version"

    status, body = _request(base, "POST", "/keys/rotate")
    assert status == 200
    v1 = body["version"]["version_id"]

    status, body = _request(base, "POST", "/encrypt", {"plaintext": "first secret"})
    assert status == 200 and body["version_id"] == v1
    ct1 = body["ciphertext"]

    status, body = _request(base, "POST", "/keys/rotate")
    v2 = body["version"]["version_id"]
    assert v1 != v2

    # 旧密文仍可解密（retired 历史版本）
    status, body = _request(base, "POST", "/decrypt", {"ciphertext": ct1})
    assert status == 200 and body["plaintext_utf8"] == "first secret"
    assert body["version_id"] == v1

    # 显式用旧版本加密 -> 409 错误版本拒绝
    status, body = _request(base, "POST", "/encrypt",
                            {"plaintext": "x", "version_id": v1})
    assert status == 409 and body["error"]["code"] == "encrypt_version_not_active"

    # 销毁旧版本后旧密文不可解密
    status, body = _request(base, "POST", f"/keys/{v1}/destroy")
    assert body["version"]["has_material"] is False
    status, body = _request(base, "POST", "/decrypt", {"ciphertext": ct1})
    assert status == 409 and body["error"]["code"] == "decrypt_version_destroyed"


def test_generate_activate_deactivate_flow(http_service):
    base, _ = http_service
    status, body = _request(base, "POST", "/keys/rotate")
    v1 = body["version"]["version_id"]
    status, body = _request(base, "POST", "/keys/generate")
    assert status == 201
    g = body["version"]["version_id"]
    assert body["version"]["state"] == "generated"

    status, _ = _request(base, "POST", f"/keys/{g}/activate")
    assert status == 200
    status, body = _request(base, "GET", f"/keys/{g}")
    assert body["version"]["active"] is True
    status, body = _request(base, "GET", f"/keys/{v1}")
    assert body["version"]["state"] == "retired"

    # retired 激活非法 -> 409
    status, body = _request(base, "POST", f"/keys/{v1}/activate")
    assert status == 409 and body["error"]["code"] == "invalid_state_transition"

    status, _ = _request(base, "POST", f"/keys/{g}/deactivate")
    assert status == 200
    status, body = _request(base, "POST", "/encrypt", {"plaintext": "z"})
    assert status == 409 and body["error"]["code"] == "no_active_version"


def test_base64_binary_payload_roundtrip(http_service):
    base, _ = http_service
    _request(base, "POST", "/keys/rotate")
    raw = bytes(range(256))
    status, body = _request(base, "POST", "/encrypt",
                            {"plaintext": base64.b64encode(raw).decode()})
    # 注意：全范围字节恰好不是合法 utf-8/可打印 base64 文本的歧义由服务端
    # base64 优先策略处理；这里直接校验解出的字节
    status2, body2 = _request(base, "POST", "/decrypt", {"ciphertext": body["ciphertext"]})
    assert status2 == 200
    assert base64.b64decode(body2["plaintext"]) == raw
    assert body2["plaintext_utf8"] is None


def test_audit_endpoint_and_error_codes(http_service):
    base, _ = http_service
    _request(base, "POST", "/keys/rotate")
    status, body = _request(base, "POST", "/encrypt", {"plaintext": "audit-me"})
    assert status == 200
    status, body = _request(base, "GET", "/audit")
    assert status == 200 and body["count"] >= 2
    # 审计响应中没有明文字段
    serialized = json.dumps(body)
    assert "audit-me" not in serialized
    actions = {e["action"] for e in body["entries"]}
    assert {"activate", "encrypt"} <= actions

    # 未知路径 / 非法 JSON / 版本不存在
    assert _request(base, "GET", "/nope")[0] == 404
    req = urllib.request.Request(base + "/encrypt", data=b"{bad json",
                                 headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 400
    assert _request(base, "GET", "/keys/v9999-zzzz")[0] == 404
