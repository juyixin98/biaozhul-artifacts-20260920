"""End-to-end HTTP tests against the real stdlib server on an ephemeral port."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

from checkpoint_service.api import build_server


@pytest.fixture()
def server(tmp_path: str):
    httpd = build_server("127.0.0.1", 0, str(tmp_path))
    port = httpd.server_address[1]
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


def _request(base: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


@pytest.mark.integration
def test_health(server: str) -> None:
    status, body = _request(server, "GET", "/health")
    assert status == 200
    assert body["status"] == "ok"


@pytest.mark.integration
def test_full_train_resume_workflow_over_http(server: str) -> None:
    cfg = {"n_samples": 64, "n_features": 3, "batch_size": 16, "n_epochs": 2,
           "checkpoint_every": 4}

    status, body = _request(server, "POST", "/runs/net1", cfg)
    assert status == 201
    assert body["state"]["global_step"] == 0

    status, body = _request(server, "POST", "/runs/net1/train", {"stop_after": 3})
    assert status == 200
    assert body["global_step"] == 3
    assert body["finished"] is False

    status, body = _request(server, "POST", "/runs/net1/train")
    assert status == 200
    assert body["finished"] is True
    assert body["global_step"] == 8

    status, body = _request(server, "GET", "/runs/net1/status")
    assert status == 200
    assert body["history_len"] == 8

    status, body = _request(server, "GET", "/runs/net1/checkpoint")
    assert status == 200
    assert body["has_optimizer_state"] and body["has_rng_state"] and body["has_cursor"]


@pytest.mark.integration
def test_create_duplicate_returns_409(server: str) -> None:
    _request(server, "POST", "/runs/dup", {})
    status, body = _request(server, "POST", "/runs/dup", {})
    assert status == 409
    assert body["error"] == "RunExistsError"


@pytest.mark.integration
def test_unknown_run_returns_404(server: str) -> None:
    status, body = _request(server, "GET", "/runs/nope/status")
    assert status == 404
    assert body["error"] == "RunNotFoundError"


@pytest.mark.integration
def test_invalid_config_returns_400(server: str) -> None:
    status, body = _request(server, "POST", "/runs/bad", {"batch_size": -3})
    assert status == 400
    assert body["error"] == "InvalidRequestError"


@pytest.mark.integration
def test_unknown_route_404(server: str) -> None:
    status, _ = _request(server, "GET", "/nonsense")
    assert status == 404


@pytest.mark.integration
def test_corrupt_checkpoint_returns_422(server: str, tmp_path: str) -> None:
    _request(server, "POST", "/runs/corrupt", {})
    payload = os.path.join(tmp_path, "corrupt", "checkpoint.payload")
    blob = bytearray(open(payload, "rb").read())
    blob[8] ^= 0x01
    open(payload, "wb").write(blob)

    status, body = _request(server, "GET", "/runs/corrupt/checkpoint")
    assert status == 422
    assert body["error"] == "CheckpointCorruptError"


@pytest.mark.integration
def test_runs_listing(server: str) -> None:
    _request(server, "POST", "/runs/a", {})
    status, body = _request(server, "GET", "/runs")
    assert status == 200
    assert any(r["run_id"] == "a" and r["committed"] for r in body)


@pytest.mark.integration
def test_malformed_json_body_returns_400(server: str) -> None:
    import http.client

    host, port = server.split("//")[1].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    conn.request("POST", "/runs/x", body="{not json", headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 400
    assert json.loads(resp.read().decode())["error"] == "InvalidRequestError"
    conn.close()


@pytest.mark.integration
def test_non_object_json_body_returns_400(server: str) -> None:
    status, body = _request(server, "POST", "/runs/x", [1, 2, 3])  # type: ignore[arg-type]
    assert status == 400
    assert body["error"] == "InvalidRequestError"


@pytest.mark.integration
def test_unknown_config_field_returns_400(server: str) -> None:
    status, body = _request(server, "POST", "/runs/x", {"nope": 1})
    assert status == 400


@pytest.mark.integration
def test_bad_stop_after_returns_400(server: str) -> None:
    _request(server, "POST", "/runs/x", {})
    status, body = _request(server, "POST", "/runs/x/train", {"stop_after": -2})
    assert status == 400
    assert body["error"] == "InvalidRequestError"
