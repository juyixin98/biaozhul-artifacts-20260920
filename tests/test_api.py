"""HTTP API tests using FastAPI's TestClient."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.artifacts import get_layout
from backend.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_layouts(compiled_artifacts):
    resp = client.get("/layouts")
    assert resp.status_code == 200
    names = resp.json()["contracts"]
    for expected in ("BoxV1", "BoxV2", "UpgradeableProxy", "WidenV1", "BaseV2"):
        assert expected in names


def test_get_layout(compiled_artifacts):
    resp = client.get("/layouts/BoxV1")
    assert resp.status_code == 200
    labels = [v["label"] for v in resp.json()["storage"]]
    assert labels == ["value", "owner", "name"]


def test_get_layout_missing():
    resp = client.get("/layouts/DoesNotExist")
    assert resp.status_code == 404


def test_check_raw_layouts():
    body = {
        "old_layout": get_layout("BoxV1"),
        "new_layout": get_layout("BoxV2"),
        "old_name": "BoxV1",
        "new_name": "BoxV2",
    }
    resp = client.post("/check", json=body)
    assert resp.status_code == 200
    assert resp.json()["compatible"] is True


def test_check_artifacts_compatible(compiled_artifacts):
    resp = client.post("/check-artifacts", json={"old": "BoxV1", "new": "BoxV2"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["compatible"] is True
    assert body["error_count"] == 0


def test_check_artifacts_incompatible(compiled_artifacts):
    resp = client.post("/check-artifacts", json={"old": "BoxV1", "new": "BoxBadReorder"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["compatible"] is False
    assert body["error_count"] > 0
    assert any(i["code"] == "SLOT_MOVED" for i in body["issues"])


def test_check_artifacts_unknown_contract():
    resp = client.post("/check-artifacts", json={"old": "BoxV1", "new": "Nope"})
    assert resp.status_code == 404
