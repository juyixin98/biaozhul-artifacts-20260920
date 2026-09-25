"""HTTP 端到端测试：真实启动 ThreadingHTTPServer，走 http.client。"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from encrypted_range_store.format import generate_master_key
from encrypted_range_store.server import build_server
from encrypted_range_store.service import EncryptedObjectStore


@pytest.fixture()
def server(tmp_path):
    key = generate_master_key()
    data_dir = str(tmp_path / "data")
    httpd = build_server("127.0.0.1", 0, data_dir, block_size=16, token=None)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port, data_dir, key
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=5)


def request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    result = resp.status, dict(resp.getheaders()), data
    conn.close()
    return result


def test_healthz(server):
    port, _, _ = server
    status, _, body = request(port, "GET", "/healthz")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_put_get_full_and_accept_ranges(server):
    port, _, _ = server
    payload = bytes(range(256))
    status, headers, body = request(
        port, "PUT", "/objects/o", payload,
        {"Content-Length": str(len(payload))},
    )
    assert status == 201
    assert json.loads(body)["size"] == 256

    status, headers, body = request(port, "GET", "/objects/o")
    assert status == 200
    assert body == payload
    assert headers["Accept-Ranges"] == "bytes"
    assert int(headers["Content-Length"]) == 256


def test_range_first_and_last(server):
    port, _, _ = server
    payload = bytes(range(256))
    request(port, "PUT", "/objects/o", payload,
            {"Content-Length": str(len(payload))})

    # 首字节
    status, headers, body = request(port, "GET", "/objects/o",
                                    headers={"Range": "bytes=0-0"})
    assert status == 206
    assert body == b"\x00"
    assert headers["Content-Range"] == "bytes 0-0/256"
    assert int(headers["Content-Length"]) == 1

    # 尾字节
    status, headers, body = request(port, "GET", "/objects/o",
                                    headers={"Range": "bytes=255-255"})
    assert status == 206
    assert body == b"\xff"
    assert headers["Content-Range"] == "bytes 255-255/256"

    # 跨块范围
    status, headers, body = request(port, "GET", "/objects/o",
                                    headers={"Range": "bytes=10-100"})
    assert status == 206
    assert body == payload[10:101]
    assert headers["Content-Range"] == "bytes 10-100/256"

    # 开放结尾与后缀
    status, _, body = request(port, "GET", "/objects/o",
                              headers={"Range": "bytes=250-"})
    assert body == payload[250:]
    status, _, body = request(port, "GET", "/objects/o",
                              headers={"Range": "bytes=-16"})
    assert body == payload[240:]


def test_empty_object(server):
    port, _, _ = server
    status, _, _ = request(port, "PUT", "/objects/empty", b"",
                           {"Content-Length": "0"})
    assert status == 201

    status, headers, body = request(port, "GET", "/objects/empty")
    assert status == 200
    assert body == b""
    assert int(headers["Content-Length"]) == 0

    # 空对象上的范围请求 -> 416
    status, headers, body = request(port, "GET", "/objects/empty",
                                    headers={"Range": "bytes=0-0"})
    assert status == 416
    assert "error" in json.loads(body)


def test_range_errors(server):
    port, _, _ = server
    request(port, "PUT", "/objects/o", b"x" * 32, {"Content-Length": "32"})
    status, _, _ = request(port, "GET", "/objects/o",
                           headers={"Range": "bytes=100-"})
    assert status == 416
    status, _, _ = request(port, "GET", "/objects/o",
                           headers={"Range": "bytes=abc-def"})
    assert status == 400
    status, _, _ = request(port, "GET", "/objects/o",
                           headers={"Range": "items=0-1"})
    assert status == 400


def test_missing_object_404(server):
    port, _, _ = server
    assert request(port, "GET", "/objects/ghost")[0] == 404


def test_bad_object_id_400(server):
    port, _, _ = server
    assert request(port, "PUT", "/objects/..%2Fx", b"z",
                   {"Content-Length": "1"})[0] in (400, 404)
    # 路径穿越形态：直接带斜杠不会匹配 /objects/{id}
    assert request(port, "GET", "/objects/a/b")[0] == 404
    assert request(port, "GET", "/objects/..%2Fx")[0] in (400, 404)


def test_tampered_ciphertext_409_and_no_plaintext(server):
    """验收：篡改后服务返回 409，且响应体不含任何对象明文。"""
    port, data_dir, key = server
    payload = bytes(range(64))
    request(port, "PUT", "/objects/o", payload,
            {"Content-Length": str(len(payload))})

    import os
    path = os.path.join(data_dir, "objects", "o")
    with open(path, "r+b") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(size - 20)  # 翻转靠近文件尾（最后一块密文/标签区）的一个字节
        b = f.read(1)
        f.seek(size - 20)
        f.write(bytes([b[0] ^ 0xFF]))

    status, _, body = request(port, "GET", "/objects/o")
    assert status == 409
    assert body != payload
    # 任何 payload 字节片段都不允许泄漏
    assert payload[:8] not in body
    assert json.loads(body)["error"]

    # 范围读取同样失败
    status2, _, body2 = request(port, "GET", "/objects/o",
                                headers={"Range": "bytes=0-63"})
    assert status2 == 409
    assert payload[:8] not in body2


def test_ciphertext_swap_returns_409(server, tmp_path):
    """验收：密文交换（跨对象整文件替换）后读取必须 409。"""
    port, data_dir, key = server
    request(port, "PUT", "/objects/A", b"payload-A" * 16,
            {"Content-Length": str(len(b"payload-A" * 16))})
    request(port, "PUT", "/objects/B", b"payload-B-content" * 8,
            {"Content-Length": str(len(b"payload-B-content" * 8))})

    import os
    pa = os.path.join(data_dir, "objects", "A")
    pb = os.path.join(data_dir, "objects", "B")
    with open(pb, "rb") as f:
        blob = f.read()
    with open(pa, "wb") as f:
        f.write(blob)

    status, _, body = request(port, "GET", "/objects/A")
    assert status == 409
    assert b"payload-B" not in body
    # B 自身可读
    assert request(port, "GET", "/objects/B")[2].startswith(b"payload-B")


def test_delete(server):
    port, _, _ = server
    request(port, "PUT", "/objects/o", b"abc", {"Content-Length": "3"})
    assert request(port, "DELETE", "/objects/o")[0] == 204
    assert request(port, "GET", "/objects/o")[0] == 404
    assert request(port, "DELETE", "/objects/o")[0] == 404


def test_put_without_content_length_411(server):
    import socket
    port, _, _ = server
    # 用原始 socket 构造一个没有 Content-Length 的 PUT，避免客户端自动补头。
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(
            b"PUT /objects/o HTTP/1.1\r\nHost: x\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n"
        )
        raw = s.recv(200)
    assert raw.startswith(b"HTTP/1.0 411") or raw.startswith(b"HTTP/1.1 411")


class TestTokenAuth:
    def test_token_required_and_accepted(self, tmp_path):
        httpd = build_server("127.0.0.1", 0, str(tmp_path / "d"),
                             block_size=16, token="secret-token")
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            # 无 token
            assert request(port, "GET", "/objects/x")[0] == 401
            # 错误 token
            assert request(port, "GET", "/objects/x",
                           headers={"Authorization": "Bearer wrong"})[0] == 401
            # 正确 token：对象不存在仍应是 404 而不是 401
            status, _, _ = request(
                port, "GET", "/objects/x",
                headers={"Authorization": "Bearer secret-token"},
            )
            assert status == 404
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=5)
