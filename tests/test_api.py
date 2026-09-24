"""HTTP-level tests using FastAPI's in-process ASGI client."""

from __future__ import annotations

import os

import pytest

# Point the server at an isolated data dir + key before app is imported in
# any worker that hasn't cached settings yet.
os.environ.setdefault("AUDIT_DATA_DIR", "./data-tests")
os.environ.setdefault("AUDIT_SIGNING_KEY", "./data-tests/key.pem")
os.environ.setdefault("AUDIT_CHECKPOINT_INTERVAL", "3600")

from fastapi.testclient import TestClient  # noqa: E402

from app import keys as keymod  # noqa: E402
from app.main import app, get_store  # noqa: E402


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    # Redirect on-disk location to a temp dir for the module.
    d = tmp_path_factory.mktemp("audit-http")
    with TestClient(app) as c:
        st = get_store()
        st.data_dir = d / "data"
        st.data_dir.mkdir(parents=True, exist_ok=True)
        st.records_path = st.data_dir / "records.jsonl"
        st.checkpoints_path = st.data_dir / "checkpoints.jsonl"
        st.records, st.checkpoints = [], []
        st.ensure_genesis_anchor()
        yield c

def test_health_and_empty_state(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["records"] == 0


def test_append_list_export_verify_roundtrip(client):
    for i in range(3):
        r = client.post(
            "/records",
            json={"actor": "alice", "action": f"do-{i}", "resource": "/x",
                  "payload": {"i": i}},
        )
        assert r.status_code == 201
        assert r.json()["record"]["seq"] == i + 1

    r = client.post("/checkpoints")
    assert r.status_code == 201
    assert r.json()["checkpoint"]["seq"] == 3

    bundle = client.get("/export").json()
    anchor = client.get("/anchor/public-key").json()["public_key_hex"]

    res = client.post("/verify", json={"trust_anchor": anchor, "bundle": bundle})
    assert res.status_code == 200
    assert res.json()["status"] == "VALID", res.json()


def test_http_detects_tampering(client):
    bundle = client.get("/export").json()
    anchor = client.get("/anchor/public-key").json()["public_key_hex"]
    bundle["records"][0]["action"] = "hacked"
    res = client.post("/verify", json={"trust_anchor": anchor, "bundle": bundle})
    assert res.json()["status"] == "TAMPERED"


def test_http_range_query(client):
    r = client.get("/records", params={"start": 1, "end": 2})
    assert r.status_code == 200
    assert [x["seq"] for x in r.json()["records"]] == [1, 2]

    r = client.get("/records", params={"start": 3, "end": 2})
    assert r.status_code == 422


def test_http_undetermined_clean_tail(client):
    client.post("/records", json={"actor": "b", "action": "late"})
    bundle = client.get("/export").json()
    anchor = client.get("/anchor/public-key").json()["public_key_hex"]
    res = client.post("/verify", json={"trust_anchor": anchor, "bundle": bundle})
    assert res.json()["status"] == "UNDETERMINED", res.json()
