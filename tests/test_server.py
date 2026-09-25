"""End-to-end tests against the real localhost HTTP server.

Spins up ThreadingHTTPServer on an ephemeral port in a background thread and
exercises the acceptance scenarios over HTTP, including a tampered object.
"""

import http.client
import json
import os
import threading
from pathlib import Path

import pytest

from enrange.server import build_server
from enrange.store import HEADER_SIZE, ObjectStore

OID = "ab" * 16


@pytest.fixture()
def server(data_dir: Path):
    store = ObjectStore.open_or_create(data_dir)
    httpd = build_server(store, host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield store, port
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    return resp, data


def test_healthz(server):
    _, port = server
    resp, data = _request(port, "GET", "/healthz")
    assert resp.status == 200
    assert json.loads(data)["status"] == "ok"


def test_post_creates_and_get_full(server):
    _, port = server
    payload = bytes(range(256))
    resp, data = _request(
        port,
        "POST",
        "/objects?block_size=64",
        body=payload,
        headers={"Content-Length": str(len(payload))},
    )
    assert resp.status == 201
    meta = json.loads(data)
    assert meta["length"] == 256
    oid = meta["id"]

    resp, data = _request(port, "GET", f"/objects/{oid}")
    assert resp.status == 200
    assert data == payload
    assert resp.getheader("Content-Length") == "256"


def test_put_explicit_id_and_ranged_reads(server):
    _, port = server
    payload = bytes((i * 7) % 256 for i in range(35))
    resp, _ = _request(
        port,
        "PUT",
        f"/objects/{OID}?block_size=10",
        body=payload,
        headers={"Content-Length": str(len(payload))},
    )
    assert resp.status == 201

    # First byte via query API.
    resp, data = _request(port, "GET", f"/objects/{OID}?start=0&end=1")
    assert resp.status == 206
    assert data == payload[:1]
    assert resp.getheader("Content-Range") == f"bytes 0-0/35"

    # Last byte (short final block) via Range header (inclusive).
    resp, data = _request(port, "GET", f"/objects/{OID}", headers={"Range": "bytes=34-34"})
    assert resp.status == 206
    assert data == payload[34:]
    assert resp.getheader("Content-Range") == f"bytes 34-34/35"

    # Open-ended HTTP range.
    resp, data = _request(port, "GET", f"/objects/{OID}", headers={"Range": "bytes=30-"})
    assert data == payload[30:]
    assert resp.getheader("Content-Range") == f"bytes 30-34/35"

    # Cross-block middle slice.
    resp, data = _request(port, "GET", f"/objects/{OID}?start=8&end=22")
    assert data == payload[8:22]


def test_empty_object_over_http(server):
    _, port = server
    resp, _ = _request(
        port, "PUT", f"/objects/{OID}", body=b"", headers={"Content-Length": "0"}
    )
    assert resp.status == 201
    resp, data = _request(port, "GET", f"/objects/{OID}")
    assert resp.status == 200 and data == b""
    resp, _ = _request(port, "HEAD", f"/objects/{OID}")
    assert resp.status == 200 and resp.getheader("Content-Length") == "0"
    resp, data = _request(port, "GET", f"/objects/{OID}/meta")
    assert json.loads(data)["blocks"] == 0


def test_head_and_meta(server):
    _, port = server
    _request(
        port, "PUT", f"/objects/{OID}?block_size=10", body=b"z" * 23,
        headers={"Content-Length": "23"},
    )
    resp, _ = _request(port, "HEAD", f"/objects/{OID}")
    assert resp.getheader("Content-Length") == "23"
    assert resp.getheader("X-Enrange-Blocks") == "3"

    resp, data = _request(port, "GET", f"/objects/{OID}/meta")
    meta = json.loads(data)
    assert meta == {"id": OID, "length": 23, "block_size": 10, "blocks": 3}


def test_list_objects(server):
    _, port = server
    _request(port, "PUT", f"/objects/{OID}", body=b"abc", headers={"Content-Length": "3"})
    resp, data = _request(port, "GET", "/objects")
    ids = json.loads(data)["objects"]
    assert OID in ids


def test_404_for_missing(server):
    _, port = server
    resp, _ = _request(port, "GET", f"/objects/{'ff' * 16}")
    assert resp.status == 404


def test_bad_range_416(server):
    _, port = server
    _request(
        port, "PUT", f"/objects/{OID}?block_size=10", body=b"q" * 10,
        headers={"Content-Length": "10"},
    )
    resp, data = _request(port, "GET", f"/objects/{OID}?start=5&end=11")
    assert resp.status == 416
    assert json.loads(data)["error"] == "bad_range"

    resp, _ = _request(port, "GET", f"/objects/{OID}", headers={"Range": "bytes=10-20"})
    assert resp.status == 416


def test_multi_range_rejected_400(server):
    _, port = server
    _request(
        port, "PUT", f"/objects/{OID}", body=b"q" * 40, headers={"Content-Length": "40"}
    )
    resp, _ = _request(port, "GET", f"/objects/{OID}", headers={"Range": "bytes=0-1,3-4"})
    assert resp.status in (400, 416)


def test_content_length_required_411(server):
    _, port = server
    # Send without Content-Length; http.client with body sets it automatically,
    # so hit a bare PUT with empty body and no header via a raw socket.
    import socket

    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(b"PUT /objects/" + OID.encode() + b" HTTP/1.1\r\nHost: x\r\n\r\n")
    response = sock.recv(4096).decode()
    sock.close()
    assert "411" in response.split("\r\n", 1)[0]


def test_tampered_object_returns_409_and_no_plaintext(server):
    store, port = server
    payload = b"A" * 10 + b"B" * 10 + b"C" * 3
    store.put(OID, payload, block_size=10)

    path = store._path(OID)
    raw = bytearray(path.read_bytes())
    raw[HEADER_SIZE + 1] ^= 0x01  # flip ciphertext bit in block 0
    path.write_bytes(raw)

    # Full GET: 409, and the error body contains no object bytes.
    resp, data = _request(port, "GET", f"/objects/{OID}")
    assert resp.status == 409
    err = json.loads(data)
    assert err["error"] == "authentication_failed"
    assert b"A" not in data and b"B" not in data and b"C" not in data

    # Range touching tampered block also 409.
    resp, _ = _request(port, "GET", f"/objects/{OID}?start=0&end=5")
    assert resp.status == 409

    # Ranged read confined to the verified tail still succeeds.
    resp, data = _request(port, "GET", f"/objects/{OID}?start=20&end=23")
    assert resp.status == 206
    assert data == b"CCC"


def test_tampered_header_409(server):
    store, port = server
    store.put(OID, b"x" * 23, block_size=10)
    path = store._path(OID)
    raw = bytearray(path.read_bytes())
    import struct

    struct.pack_into(">Q", raw, 13, 999)
    path.write_bytes(raw)
    resp, _ = _request(port, "GET", f"/objects/{OID}")
    assert resp.status == 409
    resp, _ = _request(port, "HEAD", f"/objects/{OID}")
    assert resp.status == 409


def test_ciphertext_swap_over_http_409(server):
    store, port = server
    payload = b"".join(bytes([c]) * 10 for c in b"ABCD")
    store.put(OID, payload, block_size=10)
    path = store._path(OID)
    raw = bytearray(path.read_bytes())
    frame = 26
    raw[HEADER_SIZE : HEADER_SIZE + frame], raw[
        HEADER_SIZE + frame : HEADER_SIZE + 2 * frame
    ] = (
        raw[HEADER_SIZE + frame : HEADER_SIZE + 2 * frame],
        raw[HEADER_SIZE : HEADER_SIZE + frame],
    )
    path.write_bytes(raw)
    resp, data = _request(port, "GET", f"/objects/{OID}")
    assert resp.status == 409
    assert b"AAAA" not in data and b"BBBB" not in data


def test_bad_block_size_400(server):
    _, port = server
    resp, _ = _request(
        port, "PUT", f"/objects/{OID}?block_size=0", body=b"x",
        headers={"Content-Length": "1"},
    )
    assert resp.status == 400
