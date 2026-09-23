"""End-to-end HTTP tests via FastAPI TestClient (no DDS)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rosreplay.api import create_app


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_bag_info(client):
    r = client.post("/bags/info", json={"uri": "demo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message_count"] == 43
    assert "/tick" in body["topics"]


def test_bag_info_unknown_bag_400(client):
    r = client.post("/bags/info", json={"uri": "nope"})
    assert r.status_code == 400


def test_create_session_and_publish(client, helpers):
    r = client.post(
        "/sessions", json={"uri": "demo", "rate": 2000.0, "play": True}
    )
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]

    def done():
        return client.get(f"/sessions/{sid}/status").json()["state"] == "finished"

    assert helpers.wait_until(done, timeout=3)
    pub = client.get(f"/sessions/{sid}/published").json()
    assert pub["count"] == 43


def test_pause_resume_flow(client, helpers):
    sid = client.post(
        "/sessions", json={"uri": "demo", "rate": 50, "play": True}
    ).json()["session_id"]
    helpers.wait_until(
        lambda: client.get(f"/sessions/{sid}/published").json()["count"] >= 5,
        timeout=3,
    )
    assert client.post(f"/sessions/{sid}/pause").status_code == 200
    n1 = client.get(f"/sessions/{sid}/published").json()["count"]
    import time

    time.sleep(0.15)
    assert client.get(f"/sessions/{sid}/published").json()["count"] == n1
    client.post(f"/sessions/{sid}/resume")
    client.post(f"/sessions/{sid}/rate", json={"rate": 5000})
    assert helpers.wait_until(
        lambda: client.get(f"/sessions/{sid}/status").json()["state"] == "finished",
        timeout=3,
    )


def test_seek_endpoint_generation_guard(client, helpers):
    sid = client.post(
        "/sessions", json={"uri": "demo", "rate": 50, "play": True}
    ).json()["session_id"]
    helpers.wait_until(
        lambda: client.get(f"/sessions/{sid}/published").json()["count"] >= 3,
        timeout=3,
    )
    r = client.post(f"/sessions/{sid}/seek", json={"seq": 40, "play": True})
    assert r.status_code == 200, r.text
    new_gen = r.json()["generation"]
    client.post(f"/sessions/{sid}/rate", json={"rate": 5000})
    helpers.wait_until(
        lambda: client.get(f"/sessions/{sid}/status").json()["state"] == "finished",
        timeout=3,
    )
    old = client.get(
        f"/sessions/{sid}/published", params={"generation": new_gen - 1}
    ).json()
    assert all(rec["seq"] < 40 for rec in old["records"])
    new = client.get(
        f"/sessions/{sid}/published", params={"generation": new_gen}
    ).json()
    assert min(rec["seq"] for rec in new["records"]) >= 40


def test_seek_requires_argument(client):
    sid = client.post(
        "/sessions", json={"uri": "demo", "play": False}
    ).json()["session_id"]
    r = client.post(f"/sessions/{sid}/seek", json={})
    assert r.status_code == 422


def test_unknown_session_404(client):
    assert client.get("/sessions/ghost/status").status_code == 404


def test_invalid_topic_filter_400(client):
    r = client.post(
        "/sessions", json={"uri": "demo", "topics": ["/nope"], "play": False}
    )
    assert r.status_code == 400


def test_checkpoint_save_restore_http(client, helpers, tmp_bag_root):
    sid = client.post(
        "/sessions", json={"uri": "demo", "rate": 50, "play": True}
    ).json()["session_id"]
    helpers.wait_until(
        lambda: client.get(f"/sessions/{sid}/published").json()["count"] >= 8,
        timeout=3,
    )
    client.post(f"/sessions/{sid}/pause")
    cp = client.post(
        f"/sessions/{sid}/checkpoints", json={"checkpoint_id": "http_cp"}
    ).json()
    assert cp["position"]["next_seq"] >= 8

    r = client.post(
        "/checkpoints/restore",
        json={"checkpoint_id": "http_cp", "play": False},
    )
    assert r.status_code == 200, r.text
    new_sid = r.json()["session_id"]
    assert r.json()["status"]["next_seq"] == cp["position"]["next_seq"]
    assert new_sid != sid


def test_restore_rejects_after_tamper_http(client, tmp_bag_root):
    client.post(
        "/sessions", json={"uri": "demo", "play": False, "topics": ["/tick"]}
    )
    # create a checkpoint through a tick session
    sid = client.post(
        "/sessions", json={"uri": "demo", "play": False}
    ).json()["session_id"]
    client.post(
        f"/sessions/{sid}/checkpoints", json={"checkpoint_id": "tamper_http"}
    )
    with open(tmp_bag_root / "demo" / "metadata.yaml", "a") as fh:
        fh.write("# x\n")
    r = client.post(
        "/checkpoints/restore", json={"checkpoint_id": "tamper_http"}
    )
    assert r.status_code == 409
    assert "source bag changed" in r.json()["detail"]


def test_list_and_get_checkpoint(client):
    sid = client.post(
        "/sessions", json={"uri": "demo", "play": False}
    ).json()["session_id"]
    client.post(
        f"/sessions/{sid}/checkpoints", json={"checkpoint_id": "listed"}
    )
    ids = [c["checkpoint_id"] for c in client.get("/checkpoints").json()["checkpoints"]]
    assert "listed" in ids
    assert client.get("/checkpoints/listed").status_code == 200


def test_delete_session(client):
    sid = client.post(
        "/sessions", json={"uri": "demo", "play": False}
    ).json()["session_id"]
    assert client.delete(f"/sessions/{sid}").status_code == 200
    assert client.get(f"/sessions/{sid}/status").status_code == 404
