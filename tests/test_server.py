"""End-to-end HTTP tests against an in-process server on an ephemeral port."""
import json
import urllib.error
import urllib.request

import pytest

from modelswitch.loader import load_candidate
from modelswitch.registry import ModelRegistry
from modelswitch.server import build_server


@pytest.fixture()
def server(v1_dir):
    registry = ModelRegistry()
    registry.switch(load_candidate(v1_dir))
    httpd = build_server("127.0.0.1", 0, registry)
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _request(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health_and_version(server):
    status, body = _request(server, "GET", "/health")
    assert status == 200 and body["status"] == "ok"
    status, body = _request(server, "GET", "/version")
    assert status == 200 and body["version"] == "v1"


def test_predict(server):
    status, body = _request(server, "POST", "/predict", {"x": [[0.0] * 8]})
    assert status == 200
    assert body["version"] == "v1"
    assert len(body["y"][0]) == 3
    assert abs(sum(body["y"][0]) - 1.0) < 1e-9


def test_predict_bad_shape_rejected(server):
    status, body = _request(server, "POST", "/predict", {"x": [[0.0] * 4]})
    assert status == 400
    assert "error" in body


def test_switch_and_rollback_over_http(server, v2_dir, artifact_factory):
    bad_dir = artifact_factory("v-bad", seed=505, weight_scale=1e308)

    status, body = _request(server, "POST", "/admin/switch", {"path": str(v2_dir)})
    assert status == 200 and body["version"] == "v2"

    status, body = _request(server, "POST", "/predict", {"x": [[0.0] * 8]})
    assert body["version"] == "v2"

    # Warm-up failure: rejected, active version untouched.
    status, body = _request(server, "POST", "/admin/switch", {"path": str(bad_dir)})
    assert status == 409
    assert "WarmupError" in body["error"]
    assert body["active_version"] == "v2"

    status, body = _request(server, "POST", "/admin/rollback")
    assert status == 200 and body["version"] == "v1"

    status, body = _request(server, "GET", "/admin/stats")
    assert status == 200
    assert body["active"]["version"] == "v1"
    assert body["standby"]["version"] == "v2"
