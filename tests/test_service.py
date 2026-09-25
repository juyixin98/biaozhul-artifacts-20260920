"""Tests for the local HTTP service."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from group_split.service import make_server


@pytest.fixture()
def server_url():
    server = make_server(port=0)  # ephemeral port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post(url: str, payload: dict) -> tuple[int, dict]:
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


SAMPLE_PAYLOAD = {
    "groups": ["g1", "g1", "g2", "g2", "g3", "g3", "g4", "g4"],
    "labels": [0, 1, 0, 0, 1, 0, 0, 1],
    "splits": {"train": 0.5, "test": 0.5},
    "seed": 42,
}


def test_health(server_url):
    with urllib.request.urlopen(f"{server_url}/health") as resp:
        assert resp.status == 200
        assert json.loads(resp.read()) == {"status": "ok"}


def test_split_roundtrip(server_url):
    status, body = post(f"{server_url}/split", SAMPLE_PAYLOAD)
    assert status == 200
    assert set(body) == {"assignment", "split_sizes", "report"}
    # Group isolation visible in the response itself.
    assert set(body["assignment"]) == {"g1", "g2", "g3", "g4"}
    assert sum(body["split_sizes"].values()) == len(SAMPLE_PAYLOAD["groups"])
    assert body["report"]["n_samples"] == len(SAMPLE_PAYLOAD["groups"])


def test_split_is_deterministic_over_http(server_url):
    _, first = post(f"{server_url}/split", SAMPLE_PAYLOAD)
    _, second = post(f"{server_url}/split", SAMPLE_PAYLOAD)
    assert first == second


def test_sample_splits_optional(server_url):
    payload = {**SAMPLE_PAYLOAD, "include_sample_splits": True}
    status, body = post(f"{server_url}/split", payload)
    assert status == 200
    assert len(body["sample_splits"]) == len(SAMPLE_PAYLOAD["groups"])
    # Every sample's split matches its group's assignment.
    for sample_split, group in zip(body["sample_splits"], SAMPLE_PAYLOAD["groups"]):
        assert body["assignment"][group] == sample_split


def test_invalid_payload_returns_400(server_url):
    status, body = post(f"{server_url}/split", {"groups": [1], "labels": [0]})
    assert status == 400
    assert "error" in body


def test_mismatched_lengths_return_400(server_url):
    payload = {**SAMPLE_PAYLOAD, "labels": [0]}
    status, body = post(f"{server_url}/split", payload)
    assert status == 400
    assert "length mismatch" in body["error"]


def test_unknown_path_returns_404(server_url):
    status, _ = post(f"{server_url}/nope", SAMPLE_PAYLOAD)
    assert status == 404
