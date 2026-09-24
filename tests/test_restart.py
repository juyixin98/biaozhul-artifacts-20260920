"""Restart persistence: after re-creating the Indexer against the same SQLite
file, indexed events/balances must equal a from-scratch canonical-chain replay.

DAG topology: BASE blocks 0..2; X extends to 4; Y forks base@2 and extends to
6. Observer follows X@4, then Y@4, restarts, then follows Y@6.
"""

from __future__ import annotations

import pytest

from app.abi import LEDGER_ABI
from app.chain import ChainClient
from app.config import DEFAULT_TEST_KEY
from app.effects import apply_forward
from app.indexer import Indexer
from app.store import IndexStore

from .conftest import build_tx, send_mined
from .test_reorg import deploy_on

BOB = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def canonical_replay(client: ChainClient) -> tuple[list[dict], dict[str, int]]:
    head = client.head()
    logs = client.fetch_logs_chunked(0, head.number)
    balances: dict[str, int] = {}
    for ev in logs:
        apply_forward(balances, ev)
    return logs, dict(sorted(balances.items()))


def assert_matches_replay(store: IndexStore, client: ChainClient) -> None:
    logs, balances = canonical_replay(client)
    indexed = store.list_events()
    assert len(indexed) == len(logs)
    for got, want in zip(indexed, logs):
        assert got["block_hash"] == want["block_hash"]
        assert got["tx_hash"] == want["tx_hash"]
        assert got["log_index"] == want["log_index"]
        assert got["name"] == want["name"]
        assert int(got["amount"]) == want["amount"]
        assert int(got["tag"]) == want["tag"]
    assert store.list_balances() == balances


@pytest.mark.anvil
def test_restart_after_reorg_equals_canonical_replay(node_a, node_factory, tmp_path):
    observer = node_a
    db = tmp_path / "idx.db"

    base = node_factory()
    address, ledger, acct = deploy_on(base)
    send_mined(
        base.w3, acct, build_tx(base.w3, acct, ledger, "deposit", [1000, 1]), base
    )

    node_x = node_factory(fork_url=base.rpc, fork_block=2)
    lx = node_x.w3.eth.contract(address=address, abi=LEDGER_ABI)
    ax = node_x.w3.eth.account.from_key(acct.key)
    send_mined(node_x.w3, ax, build_tx(node_x.w3, ax, lx, "deposit", [700, 7001]), node_x)
    send_mined(
        node_x.w3, ax, build_tx(node_x.w3, ax, lx, "transfer", [BOB, 120, 7003]), node_x
    )

    node_y = node_factory(fork_url=base.rpc, fork_block=2)
    ly = node_y.w3.eth.contract(address=address, abi=LEDGER_ABI)
    ay = node_y.w3.eth.account.from_key(acct.key)
    send_mined(node_y.w3, ay, build_tx(node_y.w3, ay, ly, "deposit", [400, 4001]), node_y)
    send_mined(
        node_y.w3, ay, build_tx(node_y.w3, ay, ly, "transfer", [BOB, 40, 4002]), node_y
    )

    indexer = Indexer(
        ChainClient(observer.rpc, address), IndexStore(db), start_block=0
    )
    observer.reset(node_x.rpc, fork_block=4)
    indexer.sync_once()
    assert {e["tag"] for e in indexer.store.list_events()} == {"1", "7001", "7003"}

    # Reorg onto Y@4.
    observer.reset(node_y.rpc, fork_block=4)
    indexer.sync_once()

    # Simulate process restart: fresh handles over the same DB file.
    del indexer
    restarted = Indexer(
        ChainClient(observer.rpc, address), IndexStore(db), start_block=0
    )
    report = restarted.sync_once()
    assert report.detached_events == 0 and report.ingested_events == 0
    assert_matches_replay(restarted.store, restarted.client)

    # Y extends after restart (blocks 5-6).
    send_mined(
        node_y.w3, ay, build_tx(node_y.w3, ay, ly, "withdraw", [10, 4003]), node_y
    )
    send_mined(
        node_y.w3, ay, build_tx(node_y.w3, ay, ly, "deposit", [250, 2501]), node_y
    )
    observer.reset(node_y.rpc, fork_block=6)
    restarted.sync_once()
    assert_matches_replay(restarted.store, restarted.client)

    # A fresh empty-DB indexer must converge to the identical view.
    fresh = Indexer(
        ChainClient(observer.rpc, address),
        IndexStore(tmp_path / "fresh.db"),
        start_block=0,
    )
    fresh.sync_once()
    assert fresh.store.list_events() == restarted.store.list_events()
    assert fresh.store.list_balances() == restarted.store.list_balances()
