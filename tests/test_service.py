"""本地 HTTP 服务测试 (回环端口上真实收发 HTTP)。"""

import base64
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sam.keys import (
    generate_private_key,
    private_key_to_pem,
    public_key_id,
    public_key_to_pem,
)
from sam.service import make_handler, _State


@pytest.fixture
def server(tmp_path: Path):
    owner = generate_private_key()
    key_path = tmp_path / "signing.pem"
    key_path.write_bytes(private_key_to_pem(owner))
    state = _State(str(key_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.server_address
    yield f"http://{host}:{port}", owner
    httpd.shutdown()
    httpd.server_close()


def _post(base: str, path: str, payload: dict):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def test_healthz(server):
    base, _ = server
    status, body = _get(base, "/healthz")
    assert status == 200 and body["ok"] is True and body["signing_enabled"] is True


def test_sign_then_verify_roundtrip(server):
    base, owner = server
    pub_b64 = base64.b64encode(public_key_to_pem(owner.public_key())).decode()
    files = {
        "bin/app.py": {"text": "print('hi')\n"},
        "data/a.txt": {"b64": base64.b64encode(b"hello").decode()},
    }
    status, body = _post(
        base, "/v1/sign", {"files": files, "entrypoint": "bin/app.py"}
    )
    assert status == 200 and body["ok"]
    envelope = body["envelope"]
    assert envelope["manifest"]["key_id"] == public_key_id(owner.public_key())

    status, body = _post(
        base,
        "/v1/verify",
        {"envelope": envelope, "trusted_public_keys": [pub_b64], "files": files},
    )
    assert status == 200
    assert body["verify"]["ok"] is True, body["verify"]


def test_verify_detects_tampered_content(server):
    base, owner = server
    pub_b64 = base64.b64encode(public_key_to_pem(owner.public_key())).decode()
    files = {"a.txt": {"text": "original"}}
    _, body = _post(base, "/v1/sign", {"files": files})
    tampered = {"a.txt": {"text": "TAMPERED"}}
    status, body = _post(
        base,
        "/v1/verify",
        {
            "envelope": body["envelope"],
            "trusted_public_keys": [pub_b64],
            "files": tampered,
        },
    )
    assert status == 200
    result = body["verify"]
    assert result["ok"] is False
    assert any(p["code"] == "DIGEST_MISMATCH" for p in result["problems"])


def test_verify_unknown_key(server):
    base, _ = server
    files = {"a.txt": {"text": "x"}}
    _, body = _post(base, "/v1/sign", {"files": files})
    stranger_pem = public_key_to_pem(generate_private_key().public_key())
    payload = {
        "envelope": body["envelope"],
        "trusted_public_keys": [base64.b64encode(stranger_pem).decode()],
        "files": files,
    }
    status, body = _post(base, "/v1/verify", payload)
    assert status == 200
    codes = [p["code"] for p in body["verify"]["problems"]]
    assert "UNKNOWN_KEY" in codes


def test_verify_path_traversal_in_request_blocked(server):
    base, owner = server
    pub_b64 = base64.b64encode(public_key_to_pem(owner.public_key())).decode()
    # 直接构造封套, 使 files 含穿越路径; 物化阶段就应 400
    files = {"a.txt": {"text": "x"}}
    _, body = _post(base, "/v1/sign", {"files": files})
    evil_files = {"../escape.txt": {"text": "x"}}
    status, body = _post(
        base,
        "/v1/verify",
        {
            "envelope": body["envelope"],
            "trusted_public_keys": [pub_b64],
            "files": evil_files,
        },
    )
    assert status == 400 and "逃逸" in body["error"]


def test_bad_json_returns_400(server):
    base, _ = server
    req = urllib.request.Request(
        base + "/v1/verify",
        data=b"notjson",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 400


def test_sign_disabled_without_key(tmp_path: Path):
    state = _State(None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = "http://%s:%s" % httpd.server_address
    try:
        status, body = _post(base, "/v1/sign", {"files": {}})
        assert status == 403 and "禁用" in body["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()
