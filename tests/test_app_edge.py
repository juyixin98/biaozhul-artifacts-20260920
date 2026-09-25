"""HTTP edge cases: validation envelopes and predict-before-activate."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from model_switch.app import build_server


@pytest.fixture
def server(registry_root):
    srv = build_server("127.0.0.1", 0, str(registry_root))  # no initial model
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address
    yield srv, f"http://{host}:{port}"
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _request(base, method, path, body=None, raw=None, content_type="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": content_type} if data is not None else {}
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_predict_before_activation_is_409_envelope(server):
    _, base = server
    status, payload = _request(base, "POST", "/predict", {"input": [0, 0, 0, 0]})
    assert status == 409
    assert payload["success"] is False
    assert payload["error"]["code"] == "NoActiveModelError"
    assert payload["error"]["still_serving"] is None


def test_health_ok_even_without_model(server):
    _, base = server
    status, payload = _request(base, "GET", "/health")
    assert status == 200 and payload["data"]["status"] == "ok"


def test_status_reports_no_active(server):
    _, base = server
    _, payload = _request(base, "GET", "/status")
    assert payload["data"]["active_version"] is None


def test_switch_missing_version_field(server):
    _, base = server
    status, payload = _request(base, "POST", "/switch", {"not_version": 1})
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


def test_switch_empty_body(server):
    _, base = server
    status, payload = _request(base, "POST", "/switch", raw=b"", content_type="application/json")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


def test_switch_non_string_version(server):
    _, base = server
    status, payload = _request(base, "POST", "/switch", {"version": 123})
    assert status == 400 and payload["error"]["code"] == "bad_request"


def test_predict_non_numeric_input(server):
    _, base = server
    _request(base, "POST", "/switch", {"version": "v1"})
    status, payload = _request(base, "POST", "/predict", {"input": ["a", "b", "c", "d"]})
    assert status == 400 and payload["error"]["code"] == "bad_input"


def test_predict_non_finite_input(server):
    _, base = server
    _request(base, "POST", "/switch", {"version": "v1"})
    status, payload = _request(base, "POST", "/predict", {"input": [1, 2, 3, "Infinity"]})
    assert status == 400 and payload["error"]["code"] == "bad_input"


def test_body_must_be_object(server):
    _, base = server
    status, payload = _request(base, "POST", "/predict", raw=b"[1,2,3,4]")
    assert status == 400 and payload["error"]["code"] == "bad_request"


def test_body_too_large(server):
    _, base = server
    big = json.dumps({"input": [0] * (1 << 19)})
    status, payload = _request(base, "POST", "/predict", raw=big.encode())
    assert status == 413 and payload["error"]["code"] == "body_too_large"


def test_versions_lists_empty_registry_root(tmp_path):
    # A registry dir with no valid artifacts still answers cleanly.
    srv = build_server("127.0.0.1", 0, str(tmp_path / "empty"))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = srv.server_address
        status, payload = _request(f"http://{host}:{port}", "GET", "/versions")
        assert status == 200 and payload["data"]["versions"] == []
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
