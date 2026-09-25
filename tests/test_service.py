"""End-to-end HTTP tests against the real stdlib server (real sockets)."""
import json
import threading
import urllib.request
from contextlib import closing

import pytest

from grouped_splitter.service import build_server


@pytest.fixture
def server():
    httpd = build_server("127.0.0.1", 0)  # ephemeral port
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with closing(urllib.request.urlopen(req, timeout=10)) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_raw(base, path, raw_bytes):
    req = urllib.request.Request(
        base + path,
        data=raw_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
    raise AssertionError("expected HTTP error")


def test_health(server):
    with closing(urllib.request.urlopen(server + "/health", timeout=5)) as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["status"] == "ok"


def test_split_endpoint_group_isolation_and_conservation(server):
    groups = [f"g{i // 5}" for i in range(100)]
    labels = [i % 3 for i in range(100)]
    status, body = _post(server, "/split", {
        "groups": groups, "labels": labels, "seed": 42,
    })
    assert status == 200
    assert body["n_samples"] == 100
    assert sum(body["split_sizes"].values()) == 100

    # Group isolation across the returned full assignment.
    owner = {}
    for name, indices in body["assignments"].items():
        for i in indices:
            gid = groups[i]
            assert owner.setdefault(gid, name) == name

    # Determinism: same seed -> identical body.
    _, body2 = _post(server, "/split", {"groups": groups, "labels": labels, "seed": 42})
    assert body2["assignments"] == body["assignments"]


def test_split_endpoint_rejects_malformed_body(server):
    status, body = _post_raw(server, "/split", b"{not json")
    assert status == 400
    assert "invalid JSON" in body["error"]


def test_split_endpoint_validates_ratios(server):
    status, body = _post(server, "/split", {
        "groups": ["a"] * 4, "labels": [0] * 4, "ratios": [0.5, 0.4],
    })
    assert status == 422
    assert "sum to 1" in body["error"]


def test_demo_endpoint_runs_full_pipeline(server):
    status, body = _post(server, "/demo", {
        "n_samples": 500, "n_groups": 20, "seed": 7, "tolerance": 0.05,
    })
    assert status == 200
    checks = body["checks"]
    assert checks["total_samples_conserved"]
    assert checks["groups_isolated_to_one_split"]
    assert checks["splits_pairwise_disjoint"]
    assert body["split"]["seed"] == 7
    assert "model" in body
    assert body["model"]["final_loss"] <= body["model"]["initial_loss"]


def test_unknown_path_404(server):
    status, _ = _post_raw(server, "/nope", b"{}")
    assert status == 404


def test_root_lists_endpoints(server):
    with closing(urllib.request.urlopen(server + "/", timeout=5)) as resp:
        body = json.loads(resp.read())
    assert "POST /split" in body["endpoints"]
    assert "POST /demo" in body["endpoints"]


def test_unknown_get_path_404(server):
    try:
        urllib.request.urlopen(server + "/nope", timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:
        raise AssertionError("expected 404")


def test_split_rejects_non_array_fields(server):
    status, body = _post(server, "/split", {"groups": "a,b", "labels": [0, 1]})
    assert status == 400
    assert "JSON arrays" in body["error"]


def test_split_rejects_empty_groups(server):
    status, body = _post(server, "/split", {"groups": [], "labels": []})
    assert status == 400
    assert "at least one" in body["error"]


def test_split_rejects_non_object_body(server):
    status, body = _post_raw(server, "/split", b"[1,2,3]")
    assert status == 400
    assert "JSON object" in body["error"]


def test_split_rejects_empty_body(server):
    status, body = _post_raw(server, "/split", b"")
    assert status == 400


def test_demo_accepts_mapping_ratios(server):
    status, body = _post(server, "/demo", {
        "n_samples": 300, "n_groups": 12, "seed": 1,
        "ratios": {"train": 0.8, "test": 0.2},
    })
    assert status == 200
    assert body["split"]["split_names"] == ["train", "test"]
