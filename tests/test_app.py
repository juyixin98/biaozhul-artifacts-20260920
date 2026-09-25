"""End-to-end HTTP tests against a real ThreadingHTTPServer on port 0."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest

from model_switch.app import build_server
from conftest import reference_logits, VERSION_INDEX
from model_switch.model import INPUT_DIM


@pytest.fixture
def server(registry_root):
    srv = build_server("127.0.0.1", 0, str(registry_root), initial="v1")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address
    base = f"http://{host}:{port}"
    yield srv, base
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _request(base: str, method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_health(server):
    _, base = server
    status, payload = _request(base, "GET", "/health")
    assert status == 200
    assert payload["success"] is True
    assert payload["data"]["status"] == "ok"


def test_404_envelope(server):
    _, base = server
    status, payload = _request(base, "GET", "/nope")
    assert status == 404
    assert payload["success"] is False
    assert payload["error"]["code"] == "not_found"


def test_versions_and_status(server):
    _, base = server
    status, payload = _request(base, "GET", "/versions")
    assert set(payload["data"]["versions"]) >= {"v1", "v2", "v3", "v-bad-warmup"}

    status, payload = _request(base, "GET", "/status")
    assert payload["data"]["active_version"] == "v1"
    assert payload["data"]["generation"] == 1


def test_predict_single_and_batch(server):
    _, base = server
    x = np.linspace(-1, 1, INPUT_DIM).tolist()
    status, payload = _request(base, "POST", "/predict", {"input": x})
    assert status == 200
    data = payload["data"]
    assert data["version"] == "v1"
    assert len(data["logits"]) == 2
    np.testing.assert_allclose(data["logits"], reference_logits(1, np.asarray(x)), rtol=1e-5)

    status, payload = _request(base, "POST", "/predict", {"input": [x, x]})
    assert status == 200
    assert len(payload["data"]["logits"]) == 2


def test_predict_bad_shape(server):
    _, base = server
    status, payload = _request(base, "POST", "/predict", {"input": [1.0, 2.0]})
    assert status == 400
    assert payload["error"]["code"] == "bad_input"


def test_predict_bad_json(server):
    _, base = server
    req = urllib.request.Request(
        base + "/predict", data=b"{not json", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "expected HTTPError"
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode())
        assert exc.code == 400
        assert payload["error"]["code"] == "bad_json"


def test_switch_success_then_predict_new_version(server):
    _, base = server
    status, payload = _request(base, "POST", "/switch", {"version": "v2"})
    assert status == 200
    data = payload["data"]
    assert data["version"] == "v2"
    assert data["replaced"] == "v1"
    assert data["generation"] == 2
    assert {s["name"] for s in data["load"]["stages"]} >= {"manifest", "warmup"}
    assert data["load"]["bytes_verified"] > 0

    x = [0.1, 0.2, 0.3, 0.4]
    _, payload = _request(base, "POST", "/predict", {"input": x})
    assert payload["data"]["version"] == "v2"
    np.testing.assert_allclose(
        payload["data"]["logits"],
        reference_logits(VERSION_INDEX["v2"], np.asarray(x, dtype=np.float32)),
        rtol=1e-5,
    )


def test_switch_warmup_failure_rolls_back_via_http(server):
    srv, base = server
    before_status = srv.manager.status()["generation"]
    status, payload = _request(base, "POST", "/switch", {"version": "v-bad-warmup"})
    assert status == 409
    assert payload["success"] is False
    assert payload["error"]["code"] == "WarmupValidationError"
    # Rollback is observable: old version still serving, generation unchanged.
    assert payload["error"]["still_serving"] == "v1"
    assert srv.manager.active_version == "v1"
    assert srv.manager.status()["generation"] == before_status

    _, payload = _request(base, "POST", "/predict", {"input": [0.0, 0.0, 0.0, 0.0]})
    assert payload["data"]["version"] == "v1"


def test_switch_unknown_version_keeps_service(server):
    _, base = server
    status, payload = _request(base, "POST", "/switch", {"version": "ghost"})
    assert status == 409
    assert payload["error"]["code"] == "ArtifactNotFoundError"
    assert payload["error"]["still_serving"] == "v1"


def test_concurrent_predictions_during_http_switch_never_tear(server):
    srv, base = server

    stop = threading.Event()
    failures: list[str] = []

    def client() -> None:
        rng = np.random.default_rng()
        while not stop.is_set():
            x = rng.standard_normal(INPUT_DIM).astype(np.float32)
            status, payload = _request(base, "POST", "/predict", {"input": x.tolist()})
            if status != 200:
                failures.append(f"http {status}")
                continue
            data = payload["data"]
            ref = reference_logits(VERSION_INDEX[data["version"]], x)
            if not np.allclose(data["logits"], ref, rtol=1e-5, atol=1e-6):
                failures.append(f"torn weights on {data['version']}")

    threads = [threading.Thread(target=client) for _ in range(4)]
    for t in threads:
        t.start()
    for target in ("v2", "v3", "v2", "v1", "v3"):
        status, payload = _request(base, "POST", "/switch", {"version": target})
        assert status == 200, payload
    stop.set()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()
    assert failures == []
    assert srv.manager.drain_retired(timeout_s=5.0)
