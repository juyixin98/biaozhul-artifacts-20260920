"""Acceptance test: two-layer reorganization on a local Anvil fork fixture.

Node topology is an *acyclic* fork graph. anvil lazily fetches historical blocks
from its fork upstream, so a cycle (A forks B after B forked A) deadlocks RPC.
Here only the observer node is reset; branch nodes never reset and never point
back at the observer.

    BASE (blocks 0..2: genesis, deploy, base deposit)
      ├─ X (fork@2): 3 X1(+700), 4 X2(-70)
      └─ Y (fork@2): 3' Y1(+800), 4' Y2(transfer 80)
            └─ Z (fork Y@4): 5 Z1(+9000)
         Y extends: 5 W1(+5000)

    O (observer, what the indexer reads) resets to:
        X@4  -> sync
        Y@4  -> sync   (layer 1 reorg: detach X3/X4, adopt Y3/Y4)
        Z@5  -> sync   (Z1 joins)
        Y@5  -> sync   (layer 2 reorg: detach Z5, adopt Y5)
"""

from __future__ import annotations

import pytest

from app.abi import LEDGER_ABI
from app.chain import ChainClient
from app.config import DEFAULT_TEST_KEY
from app.indexer import Indexer
from app.store import IndexStore

from .conftest import build_tx, send_mined

BOB = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"  # Anvil key #1


def deploy_on(node):
    """Deploy Ledger + return (contract, account), mining exactly one block."""
    from scripts.deploy import creation_bytecode

    w3, acct = node.w3, node.w3.eth.account.from_key(DEFAULT_TEST_KEY)
    tx = {
        "from": acct.address,
        "data": creation_bytecode(),
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 2_000_000,
        "maxFeePerGas": w3.to_wei(20, "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(1, "gwei"),
        "chainId": w3.eth.chain_id,
    }
    receipt = send_mined(w3, acct, tx, node)
    return receipt["contractAddress"], w3.eth.contract(
        address=receipt["contractAddress"], abi=LEDGER_ABI
    ), acct


def make_indexer(rpc, address, tmp_path, confirmations=1):
    store = IndexStore(tmp_path / "idx.db")
    return Indexer(
        ChainClient(rpc, address), store, start_block=0, confirmations=confirmations
    ), store


@pytest.mark.anvil
def test_two_layer_reorg_orphans_and_state(node_a, node_factory, tmp_path):
    observer = node_a

    # --- immutable base: blocks 0(genesis) 1(deploy) 2(base deposit) ----
    base = node_factory()
    address, ledger_base, acct = deploy_on(base)
    send_mined(
        base.w3, acct, build_tx(base.w3, acct, ledger_base, "deposit", [1000, 1]), base
    )
    assert base.w3.eth.block_number == 2

    # --- branch X (blocks 3-4) ------------------------------------------
    node_x = node_factory(fork_url=base.rpc, fork_block=2)
    lx = node_x.w3.eth.contract(address=address, abi=LEDGER_ABI)
    ax = node_x.w3.eth.account.from_key(acct.key)
    send_mined(node_x.w3, ax, build_tx(node_x.w3, ax, lx, "deposit", [700, 7001]), node_x)
    send_mined(node_x.w3, ax, build_tx(node_x.w3, ax, lx, "withdraw", [70, 7002]), node_x)
    h_x3 = "0x" + node_x.w3.eth.get_block(3).hash.hex()
    h_x4 = "0x" + node_x.w3.eth.get_block(4).hash.hex()

    # --- branch Y (blocks 3'-4') ----------------------------------------
    node_y = node_factory(fork_url=base.rpc, fork_block=2)
    ly = node_y.w3.eth.contract(address=address, abi=LEDGER_ABI)
    ay = node_y.w3.eth.account.from_key(acct.key)
    send_mined(node_y.w3, ay, build_tx(node_y.w3, ay, ly, "deposit", [800, 8001]), node_y)
    send_mined(
        node_y.w3,
        ay,
        build_tx(node_y.w3, ay, ly, "transfer", [BOB, 80, 8002]),
        node_y,
    )

    indexer, store = make_indexer(observer.rpc, address, tmp_path)

    # Observer adopts X chain up to block 4.
    observer.reset(node_x.rpc, fork_block=4)
    r = indexer.sync_once()
    assert r.ingested_events == 3  # base deposit + X1 + X2
    assert store.list_balances()[acct.address] == 1630

    # --- layer 1: observer adopts Y chain up to block 4 -----------------
    observer.reset(node_y.rpc, fork_block=4)
    r = indexer.sync_once()
    assert r.reorg is True
    assert r.fork_point == 2
    assert len(r.detached_blocks) == 2
    assert set(r.detached_blocks) == {h_x3, h_x4}

    canonical_tags = {e["tag"] for e in store.list_events()}
    orphan_hashes = {b["hash"] for b in store.list_orphan_blocks()}
    assert "7001" not in canonical_tags and "7002" not in canonical_tags
    assert {h_x3, h_x4} <= orphan_hashes  # orphan headers retained
    assert {"8001", "8002"} <= canonical_tags
    balances = store.list_balances()
    assert balances[acct.address] == 1720  # 1000 + 800 - 80
    assert balances[BOB] == 80

    # --- layer 2: Z mines block 5; Y mines its own block 5 --------------
    node_z = node_factory(fork_url=node_y.rpc, fork_block=4)
    lz = node_z.w3.eth.contract(address=address, abi=LEDGER_ABI)
    az = node_z.w3.eth.account.from_key(acct.key)
    send_mined(node_z.w3, az, build_tx(node_z.w3, az, lz, "deposit", [9000, 9001]), node_z)
    h_z5 = "0x" + node_z.w3.eth.get_block(5).hash.hex()

    send_mined(node_y.w3, ay, build_tx(node_y.w3, ay, ly, "deposit", [5000, 5001]), node_y)
    assert node_y.w3.eth.block_number == 5

    # Z1 joins canonical.
    observer.reset(node_z.rpc, fork_block=5)
    r = indexer.sync_once()
    assert r.reorg is False
    assert store.list_balances()[acct.address] == 10720
    assert "9001" in {e["tag"] for e in store.list_events()}

    # Second reorg layer at depth 1: Y5 replaces Z5.
    observer.reset(node_y.rpc, fork_block=5)
    r = indexer.sync_once()
    assert r.reorg is True
    assert r.fork_point == 4
    assert r.detached_blocks == [h_z5]
    canonical_tags = {e["tag"] for e in store.list_events()}
    assert "9001" not in canonical_tags
    assert "5001" in canonical_tags
    balances = store.list_balances()
    assert balances[acct.address] == 6720  # 1000 + 800 - 80 + 5000
    assert balances[BOB] == 80
    # Three orphan headers retained: X3, X4, Z5; orphan event rows removed.
    assert len(store.list_orphan_blocks()) == 3
    assert store.block_counts()["blocks_canonical"] == 6  # 0..5
