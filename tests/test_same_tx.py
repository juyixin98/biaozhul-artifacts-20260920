"""Same raw transaction mined into two competing blocks must not be confused.

DAG topology (see test_reorg.py for why forks must stay acyclic):

    BASE blocks 0..2 (deploy + a 1000 deposit on block 2)
      ├─ X: block 3 mines the signed tx T (deposit 333)
      └─ Y: block 3' mines the *same raw* signed tx T

The indexer observes X@3 first, then resets to Y@3. T's tx_hash is identical
on both branches; only the block hash differs. The stored event rows must
collapse to exactly one canonical copy attached to the new block, with no
double-counted balance effect.
"""

from __future__ import annotations

import pytest

from app.abi import LEDGER_ABI
from app.chain import ChainClient
from app.config import DEFAULT_TEST_KEY
from app.indexer import Indexer
from app.store import IndexStore

from .conftest import build_tx, send_mined, send_raw_mined, signed_raw
from .test_reorg import deploy_on


@pytest.mark.anvil
def test_same_transaction_different_blocks(node_a, node_factory, tmp_path):
    observer = node_a

    base = node_factory()
    address, ledger, acct = deploy_on(base)
    send_mined(
        base.w3, acct, build_tx(base.w3, acct, ledger, "deposit", [1000, 1]), base
    )

    # One signed transaction (nonce 2 on the shared account), carried raw into
    # both branches. The two branches share the same chain ID and sender state
    # inherited from base@2, so the signature stays valid on both.
    tx = build_tx(base.w3, acct, ledger, "deposit", [333, 3331])
    raw = signed_raw(acct, tx)

    node_x = node_factory(fork_url=base.rpc, fork_block=2)
    receipt_x = send_raw_mined(node_x.w3, raw, node_x)
    tx_hash = "0x" + bytes(receipt_x["transactionHash"]).hex()
    h_x3 = "0x" + node_x.w3.eth.get_block(3).hash.hex()

    # Y's competing block carries the SAME tx T, preceded by a different tx
    # from a funded second account, so the block content/state-root differs.
    node_y = node_factory(fork_url=base.rpc, fork_block=2)
    bob_key = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"  # key #1
    bob = node_y.w3.eth.account.from_key(bob_key)
    ledger_y = node_y.w3.eth.contract(address=address, abi=LEDGER_ABI)
    # Queue without mining: Bob's tx enters the pool, then T, mined together.
    bob_tx = build_tx(node_y.w3, bob, ledger_y, "deposit", [5, 2221])
    signed_bob = bob.sign_transaction(bob_tx).raw_transaction
    node_y.w3.eth.send_raw_transaction(signed_bob)
    receipt_y = send_raw_mined(node_y.w3, raw, node_y)
    h_y3 = "0x" + bytes(receipt_y["blockHash"]).hex()
    assert h_y3 != h_x3
    assert "0x" + bytes(receipt_y["transactionHash"]).hex() == tx_hash
    # Both events landed in the same Y block; T keeps its single log index.
    y_logs = node_y.w3.eth.get_logs(
        {"fromBlock": 3, "toBlock": 3, "address": address}
    )
    assert len(y_logs) == 2

    store = IndexStore(tmp_path / "idx.db")
    indexer = Indexer(ChainClient(observer.rpc, address), store, start_block=0)

    observer.reset(node_x.rpc, fork_block=3)
    indexer.sync_once()
    before = [e for e in store.list_events() if e["tag"] == "3331"]
    assert len(before) == 1
    assert before[0]["tx_hash"] == tx_hash
    assert before[0]["block_hash"] == h_x3
    assert store.list_balances()[acct.address] == 1333

    # Reorg onto the competing branch.
    observer.reset(node_y.rpc, fork_block=3)
    indexer.sync_once()

    after = [e for e in store.list_events() if e["tag"] == "3331"]
    assert len(after) == 1  # one canonical row, re-attached to block 3'
    assert after[0]["tx_hash"] == tx_hash
    assert after[0]["block_hash"] == h_y3
    assert after[0]["block_hash"] != h_x3
    # Old copy inverted away: +333 contributes exactly once.
    assert store.list_balances()[acct.address] == 1333
    orphan_hashes = {b["hash"] for b in store.list_orphan_blocks()}
    assert h_x3 in orphan_hashes and h_y3 not in orphan_hashes
