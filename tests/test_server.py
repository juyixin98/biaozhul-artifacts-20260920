"""HTTP 接口端到端测试（真实监听 127.0.0.1 临时端口）。"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request

import pytest

from envelope.server import build_server


@pytest.fixture()
def server(tmp_path):
    srv = build_server(tmp_path / "store", "127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _request(server, method: str, path: str, payload: dict | None = None):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_full_lifecycle_and_rotation_over_http(server):
    # 还没有主密钥时加密 → 409
    status, body = _request(
        server, "POST", "/encrypt", {"data_b64": base64.b64encode(b"hi").decode()}
    )
    assert status == 409 and "密钥库为空" in body["error"]

    status, key1 = _request(server, "POST", "/keys")
    assert status == 201 and key1["kid"].startswith("mk_")

    plaintext = b"envelope over http " * 20
    status, enc = _request(
        server,
        "POST",
        "/encrypt",
        {"data_b64": base64.b64encode(plaintext).decode(), "chunk_size": 64},
    )
    assert status == 201
    fid = enc["file_id"]
    assert enc["blocks"] >= 1

    status, dec = _request(server, "GET", f"/decrypt?file_id={fid}")
    assert status == 200
    assert base64.b64decode(dec["data_b64"]) == plaintext

    status, rot = _request(server, "POST", "/rotate", {"file_id": fid})
    assert status == 200
    assert rot["old_kid"] == key1["kid"]
    assert rot["new_kid"] != key1["kid"]
    assert rot["blob_bytes_changed"] == 0

    status, dec = _request(server, "GET", f"/decrypt?file_id={fid}")
    assert status == 200
    assert base64.b64decode(dec["data_b64"]) == plaintext

    status, listing = _request(server, "GET", "/files")
    assert status == 200 and len(listing["files"]) == 1
    assert listing["files"][0]["header_version"] == 2


def test_encrypt_before_key_and_bad_requests(server):
    _request(server, "POST", "/keys")
    status, _ = _request(server, "POST", "/encrypt", {"data_b64": "!!!not-base64"})
    assert status == 400
    status, _ = _request(server, "GET", "/decrypt?file_id=")
    assert status == 400
    status, _ = _request(server, "GET", "/files/" + "z" * 32)
    assert status in (404, 400)


def test_unknown_route(server):
    status, body = _request(server, "GET", "/nope")
    assert status == 404
