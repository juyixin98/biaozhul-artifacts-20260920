"""Restart/persistence: a file-backed indexer reopened after a *process-like*
restart must equal a fresh canonical-chain replay. Also exercises HTTP API via
FastAPI's TestClient against an in-memory indexer.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app import onchain
from app.indexer import Indexer
from app.storage import Storage


def test_file_backed_restart_after_reorg(anvil, tmp_path):
    w3 = onchain.make_w3(anvil)
    gen = onchain.snapshot(w3)
    try:
        sender = onchain.TxSender(w3)
        db_path = str(tmp_path / "idx.db")

        # first "process": deploy, index, reorg, index again -- on disk
        from tests.conftest import DEPLOY_KEY, USER1_KEY

        addr = onchain.deploy_vault(w3, sender, DEPLOY_KEY)
        vault = onchain.vault_at(w3, addr)
        store1 = Storage(db_path)
        idx1 = Indexer(w3, store1, vault_address=addr, start_block=0)

        sender.vault_call(USER1_KEY, vault, "deposit", value=100)
        onchain.mine_blocks(w3, 1)  # 2
        snap2 = onchain.snapshot(w3)
        sender.vault_call(USER1_KEY, vault, "deposit", value=4242)  # uncle-only
        onchain.mine_blocks(w3, 1)  # 3u
        idx1.sync_to(3)
        assert store1.get_balance(onchain.address_for(USER1_KEY)) == 4342

        onchain.revert(w3, snap2)
        sender.reset_nonce_tracking()
        sender.vault_call(USER1_KEY, vault, "deposit", value=7)
        onchain.mine_blocks(w3, 1)  # 3c
        sender.vault_call(USER1_KEY, vault, "withdraw", 30)
        onchain.mine_blocks(w3, 1)  # 4c
        idx1.sync_to(4)

        snapshot_state = store1.debug_rows()
        snapshot_events = sorted(
            (e.block_number, e.block_hash, e.tx_hash, e.log_index, e.event_name, e.amount)
            for e in store1.list_events(limit=1000)
        )
        store1.close()

        # "restart": reopen the same file with a brand-new Storage/Indexer.
        store2 = Storage(db_path)
        idx2 = Indexer(w3, store2, vault_address=addr, start_block=0)
        # tip survived, no resync needed; status equals what was persisted
        assert store2.tip().number == 4
        rep = idx2.poll_once()  # head is 4, depth 0 -> detects any same-height swap
        assert rep.rolled_back == []
        assert store2.debug_rows() == snapshot_state
        reopened_events = sorted(
            (e.block_number, e.block_hash, e.tx_hash, e.log_index, e.event_name, e.amount)
            for e in store2.list_events(limit=1000)
        )
        assert reopened_events == snapshot_events

        # a fresh indexer replaying the canonical chain reaches identical state
        store3 = Storage(":memory:")
        idx3 = Indexer(w3, store3, vault_address=addr, start_block=0)
        idx3.sync_to(4)
        assert store3.debug_rows() == snapshot_state

        # and agrees with on-chain ground truth
        dep, wit = vault.functions.stats().call()
        assert store2.get_total("deposited") == dep == 107
        assert store2.get_total("withdrawn") == wit == 30
        assert store2.get_balance(onchain.address_for(USER1_KEY)) == 77
        store2.close()
    finally:
        onchain.revert(w3, gen)


@pytest.fixture()
def api_env(anvil, tmp_path, monkeypatch):
    w3 = onchain.make_w3(anvil)
    gen = onchain.snapshot(w3)
    sender = onchain.TxSender(w3)
    from tests.conftest import DEPLOY_KEY

    addr = onchain.deploy_vault(w3, sender, DEPLOY_KEY)
    monkeypatch.setenv("RPC_URL", anvil)
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("VAULT_ADDRESS", addr)
    monkeypatch.setenv("CONFIRMATION_DEPTH", "0")
    monkeypatch.setenv("POLL_INTERVAL", "60")  # don't race with manual /sync
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, w3, sender, addr
    onchain.revert(w3, gen)


def test_http_api_end_to_end(api_env):
    from tests.conftest import USER1_KEY

    client, w3, sender, addr = api_env
    vault = onchain.vault_at(w3, addr)

    # empty initially
    assert client.get("/health").json()["status"] == "ok"
    st = client.get("/status").json()
    assert st["confirmation_depth"] == 0
    assert st["indexed_tip_number"] is not None  # lifespan anchored+synced

    # do work on chain, then drive the indexer over HTTP
    sender.vault_call(USER1_KEY, vault, "deposit", value=500)
    onchain.mine_blocks(w3, 1)
    sender.vault_call(USER1_KEY, vault, "withdraw", 200)
    onchain.mine_blocks(w3, 1)
    rep = client.post("/sync").json()
    assert rep["rolled_back"] == []

    user1 = onchain.address_for(USER1_KEY)
    bal = client.get(f"/balance/{user1}").json()
    assert bal["balance"] == "300"
    totals = client.get("/totals").json()
    assert totals == {"deposited": "500", "withdrawn": "200", "net_locked": "300"}
    evs = client.get("/events", params={"limit": 10}).json()
    assert [e["event"] for e in evs] == ["Withdrawn", "Deposited"]
    assert all(e["block_hash"].startswith("0x") for e in evs)

    dbg = client.get("/debug").json()
    assert dbg["totals"]["deposited"] == 500
    assert dbg["balances"][user1] == 300

    # reorg the latest block away and re-sync over HTTP
    tip_before = client.get("/status").json()["indexed_tip_hash"]
    snap = onchain.snapshot(w3)
    sender.vault_call(USER1_KEY, vault, "deposit", value=999_999)
    onchain.mine_blocks(w3, 1)
    client.post("/sync")
    assert client.get("/totals").json()["deposited"] == "1000499"
    onchain.revert(w3, snap)
    sender.reset_nonce_tracking()
    onchain.mine_blocks(w3, 1)  # empty replacement block at same height
    new_head = w3.eth.get_block("latest")["hash"].to_0x_hex()
    rep = client.post("/sync").json()
    assert rep["rolled_back"] == [w3.eth.block_number]
    # totals restored to canonical truth; uncle deposit gone
    assert client.get("/totals").json()["deposited"] == "500"
    assert client.get("/status").json()["indexed_tip_hash"] == new_head
    assert client.get("/status").json()["indexed_tip_hash"] != tip_before
