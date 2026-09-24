"""API tests: offline checker routes with FastAPI TestClient, and the
on-chain deploy/upgrade routes when Anvil is reachable.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from checker.chain import get_web3

ROOT = Path(__file__).resolve().parents[2]
LAYOUTS = ROOT / "layouts"
client = TestClient(app)


def bundle(name):
    return json.loads((LAYOUTS / f"{name}.json").read_text())


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_contracts_lists_versions():
    r = client.get("/contracts")
    names = r.json()["contracts"]
    assert {"BoxV1", "BoxV2", "BoxV3", "BoxV4", "BoxV5", "BoxV6"} <= set(names)


def test_check_named_accepts_compatible():
    r = client.post("/check/named", json={"old": "BoxV1", "new": "BoxV2"})
    assert r.status_code == 200
    body = r.json()
    assert body["compatible"] is True


def test_check_named_blocks_reorder():
    r = client.post("/check/named", json={"old": "BoxV1", "new": "BoxV3"})
    body = r.json()
    assert body["compatible"] is False
    assert body["summary"]["errors"] >= 1


def test_check_raw_documents():
    r = client.post(
        "/check", json={"old": bundle("BoxV1"), "new": bundle("BoxV4")}
    )
    body = r.json()
    assert body["compatible"] is False
    assert any(e["code"] == "DYNAMIC_TYPE_CHANGED" for e in body["errors"])


def test_check_bad_document_returns_422():
    r = client.post("/check", json={"old": {"x": 1}, "new": {"x": 2}})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# On-chain routes (skipped when Anvil is not reachable)
# ---------------------------------------------------------------------------


def _chain_available():
    try:
        get_web3()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _chain_available(), reason="anvil not running")
def test_deploy_and_upgrade_flow():
    r = client.post("/deploy", json={"impl": "BoxV1", "seed": True})
    assert r.status_code == 200, r.text
    d = r.json()
    proxy = d["proxy"]
    assert d["seeded"] is True
    assert d["state"]["x"] == 123456789

    # incompatible upgrade must be blocked and NOT change implementation
    r = client.post(
        "/upgrade", json={"proxy": proxy, "new_impl": "BoxV3", "old_impl": "BoxV1"}
    )
    body = r.json()
    assert body["upgraded"] is False and body["blocked"] is True

    # compatible upgrade succeeds and preserves state
    r = client.post(
        "/upgrade", json={"proxy": proxy, "new_impl": "BoxV2", "old_impl": "BoxV1"}
    )
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["upgraded"] is True
    assert body["state_preserved"] is True
    assert body["state_after"]["x"] == 123456789
    assert body["implementation_before"] != body["implementation_after"]

    # state endpoint
    r = client.get(f"/proxy/{proxy}/state", params={"impl": "BoxV2"})
    assert r.json()["state"]["name"] == "hello-upgrade"
