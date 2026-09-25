"""Tests for the HTTP service layer (handler logic + a live socket round-trip)."""

import json
import threading
import urllib.request

import pytest

from abtest.server import build_response, make_server

VALID_PAYLOAD = {
    "control": [1.0, 2.0, 3.0, 4.0, 5.0],
    "treatment": [2.0, 3.0, 4.0, 5.0, 6.0],
    "confidence_level": 0.95,
    "missing_strategy": "drop",
}


def test_build_response_success():
    status, body = build_response(VALID_PAYLOAD)
    assert status == 200
    assert body["mean_diff"] == pytest.approx(1.0)
    assert body["ci_lower"] < body["mean_diff"] < body["ci_upper"]
    assert "validity_notice" in body
    assert "NOT valid" in body["validity_notice"]


def test_build_response_accepts_null_as_missing():
    payload = dict(VALID_PAYLOAD)
    payload["control"] = [1.0, None, 3.0, 4.0, 5.0]
    status, body = build_response(payload)
    assert status == 200
    assert body["n_missing_control"] == 1
    assert body["n_control"] == 4


def test_build_response_rejects_bad_input():
    status, _ = build_response({"control": [1.0], "treatment": [1.0, 2.0]})
    assert status == 400
    status, _ = build_response({"control": "nope", "treatment": [1.0, 2.0]})
    assert status == 400
    status, _ = build_response({"control": [1.0, "x"], "treatment": [1.0, 2.0]})
    assert status == 400
    status, _ = build_response({"control": [1.0, True], "treatment": [1.0, 2.0]})
    assert status == 400
    status, _ = build_response({"control": [1.0, 2.0], "treatment": [1.0, 2.0],
                                "confidence_level": 2.0})
    assert status == 400
    status, _ = build_response([1, 2, 3])
    assert status == 400


@pytest.fixture()
def live_server():
    server = make_server("127.0.0.1", 0)  # ephemeral port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _post(url: str, payload) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_live_health_endpoint(live_server):
    with urllib.request.urlopen(f"{live_server}/health") as resp:
        assert resp.status == 200
        assert json.loads(resp.read()) == {"status": "ok"}


def test_live_analyze_round_trip(live_server):
    status, body = _post(f"{live_server}/analyze", VALID_PAYLOAD)
    assert status == 200
    assert body["mean_diff"] == pytest.approx(1.0)


def test_live_analyze_rejects_bad_payload(live_server):
    status, body = _post(f"{live_server}/analyze", {"control": [1.0]})
    assert status == 400
    assert "error" in body


def test_live_unknown_path_404(live_server):
    status, _ = _post(f"{live_server}/nope", {})
    assert status == 404
