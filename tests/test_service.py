"""Integration tests for the HTTP service (real server on an ephemeral port)."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from pitjoin import FeatureStore
from pitjoin.service import make_handler


@pytest.fixture()
def server():
    store = FeatureStore()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def post(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=5)


def get(base, path):
    return urllib.request.urlopen(base + path, timeout=5)


def test_health(server):
    with get(server, "/health") as resp:
        assert resp.status == 200
        assert json.loads(resp.read()) == {"status": "ok"}


def test_ingest_then_join_roundtrip(server):
    with post(server, "/ingest", {"records": [
        {"entity_id": "u1", "feature": "f", "value": 1.0, "event_ts": 10, "ingest_ts": 12},
        {"entity_id": "u1", "feature": "f", "value": 2.0, "event_ts": 10, "ingest_ts": 50},
    ]}) as resp:
        assert json.loads(resp.read()) == {"ingested": 2, "total": 2}

    # At t=20 only the first version is visible; at t=60 the correction is.
    with post(server, "/join", {
        "spine": [{"entity_id": "u1", "event_ts": 20}, {"entity_id": "u1", "event_ts": 60}],
        "features": ["f"],
    }) as resp:
        results = json.loads(resp.read())["results"]
    assert [r["value"] for r in results] == [1.0, 2.0]
    assert results[0]["selected_ingest_ts"] == 12
    assert results[1]["selected_ingest_ts"] == 50
    assert all(r["reason"].startswith("SELECTED") for r in results)

    with get(server, "/stats") as resp:
        stats = json.loads(resp.read())
    assert stats["records"] == 2
    assert stats["features"] == ["f"]


def test_join_missing_feature_reports_reason(server):
    with post(server, "/join", {
        "spine": [{"entity_id": "nobody", "event_ts": 10}],
        "features": ["nope"],
    }) as resp:
        [r] = json.loads(resp.read())["results"]
    assert r["value"] is None
    assert r["reason"].startswith("MISSING")


def test_bad_request_returns_400(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(server, "/join", {"spine": [{"entity_id": "u1"}], "features": ["f"]})
    assert excinfo.value.code == 400


def test_unknown_path_returns_404(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(server, "/nope")
    assert excinfo.value.code == 404
