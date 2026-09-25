"""HTTP 服务端到端测试：真实起服务、真实发请求。"""
import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from certchain.fixtures import DEFAULT_VALIDATION_TIME, case_expired, case_good
from certchain.service import VerifyHandler


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), VerifyHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address
    yield f"http://{host}:{port}"
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _request_body(case):
    return {
        "leaf": case["leaf"],
        "intermediates": case["intermediates"],
        "trust_roots": case["trust_roots"],
        "validation_time": DEFAULT_VALIDATION_TIME,
        "purpose": "server_tls",
        "hostname": case["hostname"],
    }


def test_health(server):
    with urllib.request.urlopen(server + "/health", timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert body == {"status": "ok"}


def test_verify_good(server):
    status, body = _post(server, "/verify", _request_body(case_good()))
    assert status == 200
    assert body["valid"] is True
    assert body["revocation_checked"] is False
    assert len(body["chain"]) == 3


def test_verify_expired(server):
    status, body = _post(server, "/verify", _request_body(case_expired()))
    assert status == 200
    assert body["valid"] is False
    assert body["error"]


def test_verify_bad_json(server):
    req = urllib.request.Request(
        server + "/verify", data=b"{not json", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "应返回 400"
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_verify_missing_fields(server):
    status, body = _post(server, "/verify", {"purpose": "server_tls"})
    assert status == 400
    assert "error" in body


def test_verify_missing_validation_time(server):
    payload = _request_body(case_good())
    del payload["validation_time"]
    status, body = _post(server, "/verify", payload)
    assert status == 400
    assert "validation_time" in body["error"]


def test_not_found(server):
    status, _ = _post(server, "/nope", {})
    assert status == 404
