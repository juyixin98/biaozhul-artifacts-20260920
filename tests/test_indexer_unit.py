"""Indexer reorg logic driven by an in-memory fake chain (no Anvil needed).

Covers fork-point detection, rollback, catch-up and re-adoption of a
previously orphaned block — including two successive reorgs of differing depth.
"""

from __future__ import annotations

from app.chain import BlockHeader
from app.indexer import Indexer
from app.store import IndexStore

ALICE = "0xAA00000000000000000000000000000000000001"
BOB = "0xBB00000000000000000000000000000000000002"


class FakeChain:
    """Mutable chain: maps height -> (hash, parent_hash), plus logs per block."""

    def __init__(self):
        self.blocks: dict[int, tuple[str, str]] = {}
        self.logs: dict[tuple[int, str], list[dict]] = {}
        self.head_number = -1

    def add_block(self, number, block_hash, parent_hash, events=None):
        self.blocks[number] = (block_hash, parent_hash)
        self.head_number = max(self.head_number, number)
        if events:
            for i, e in enumerate(events):
                e.update(
                    block_number=number,
                    block_hash=block_hash,
                    log_index=i,
                    tx_hash=f"0xtx-{number}-{i}",
                )
            self.logs[(number, block_hash)] = events

    def prune_above(self, number):
        for n in list(self.blocks):
            if n > number:
                self.blocks.pop(n)
        self.logs = {k: v for k, v in self.logs.items() if k[0] <= number}
        self.head_number = number

    # ChainClient-shaped interface used by Indexer ---------------------

    def head(self):
        h, p = self.blocks[self.head_number]
        return BlockHeader(self.head_number, h, p)

    def get_header(self, number):
        h, p = self.blocks[number]
        return BlockHeader(number, h, p)

    def fetch_logs_chunked(self, lo, hi, chunk_size=100):
        out = []
        for n in range(lo, hi + 1):
            h, _ = self.blocks[n]
            out.extend(self.logs.get((n, h), []))
        return out


def dep(amount, tag, account=ALICE):
    return {"name": "Deposited", "account": account, "to_account": None,
            "amount": amount, "tag": tag}


def wd(amount, tag, account=ALICE):
    return {"name": "Withdrawn", "account": account, "to_account": None,
            "amount": amount, "tag": tag}


def xfer(amount, tag, frm=ALICE, to=BOB):
    return {"name": "Transferred", "account": frm, "to_account": to,
            "amount": amount, "tag": tag}


def make_indexer(tmp_path, chain):
    store = IndexStore(tmp_path / "fake.db")
    return Indexer(chain, store, start_block=0, confirmations=1), store


def test_two_layer_reorg_and_readopt(tmp_path):
    chain = FakeChain()
    chain.add_block(0, "0xG0", "0x")
    chain.add_block(1, "0xD1", "0xG0")
    chain.add_block(2, "0xB2", "0xD1", [dep(1000, 1)])

    # Branch X
    chain.add_block(3, "0xX3", "0xB2", [dep(700, 7001)])
    chain.add_block(4, "0xX4", "0xX3", [wd(70, 7002)])

    indexer, store = make_indexer(tmp_path, chain)
    r = indexer.sync_once()
    assert r.ingested_events == 3 and r.reorg is False
    assert store.list_balances() == {ALICE: 1630}

    # Layer 1: blocks 3-4 replaced by branch Y (fork point 2).
    chain.prune_above(2)
    chain.add_block(3, "0xY3", "0xB2", [dep(800, 8001)])
    chain.add_block(4, "0xY4", "0xY3", [xfer(80, 8002)])
    r = indexer.sync_once()
    assert r.reorg is True and r.fork_point == 2
    assert r.detached_blocks == ["0xX4", "0xX3"]
    assert store.list_balances() == {ALICE: 1720, BOB: 80}
    assert {b["hash"] for b in store.list_orphan_blocks()} == {"0xX3", "0xX4"}

    # Y extends with block 5.
    chain.add_block(5, "0xY5", "0xY4", [dep(5000, 5001)])
    r = indexer.sync_once()
    assert r.reorg is False and r.fork_point == 4
    assert store.list_balances() == {ALICE: 6720, BOB: 80}

    # Layer 2: a different block 5 (fork point 4, shallower reorg).
    chain.prune_above(4)
    chain.add_block(5, "0xZ5", "0xY4", [dep(9000, 9001)])
    r = indexer.sync_once()
    assert r.reorg is True and r.fork_point == 4
    assert r.detached_blocks == ["0xY5"]
    assert store.list_balances() == {ALICE: 10720, BOB: 80}
    canonical_tags = {e["tag"] for e in store.list_events()}
    assert "5001" not in canonical_tags and "9001" in canonical_tags

    # Flip back: Y5 is an orphan on record and must be re-adopted in place,
    # with no duplicate rows and correct balances restored.
    chain.prune_above(4)
    chain.add_block(5, "0xY5", "0xY4", [dep(5000, 5001)])
    r = indexer.sync_once()
    assert r.reorg is True and r.ingested_blocks == ["0xY5"]
    assert store.list_balances() == {ALICE: 6720, BOB: 80}
    events = store.list_events()
    assert sum(1 for e in events if e["block_hash"] == "0xY5") == 1
    assert store.block_counts()["blocks_orphan"] == 3  # X3, X4, Z5


def test_genesis_divergence_rolls_back_everything(tmp_path):
    """Even the genesis hash differs: fork point -1, blocks 0..n all detach."""
    chain = FakeChain()
    chain.add_block(0, "0xG0", "0x")
    chain.add_block(1, "0xA1", "0xG0", [dep(10, 1)])
    indexer, store = make_indexer(tmp_path, chain)
    indexer.sync_once()

    chain.prune_above(-1)
    chain.add_block(0, "0xG0new", "0x")
    chain.add_block(1, "0xC1", "0xG0new", [dep(99, 99)])
    r = indexer.sync_once()
    assert r.reorg is True and r.fork_point == -1
    assert r.detached_blocks == ["0xA1", "0xG0"]
    assert store.list_balances() == {ALICE: 99}
    assert store.block_counts()["blocks_canonical"] == 2


def test_head_shrinks_due_to_reset(tmp_path):
    """If the new head is shorter than the stored tip, rollback still works."""
    chain = FakeChain()
    chain.add_block(0, "0xG0", "0x")
    chain.add_block(1, "0xA1", "0xG0", [dep(10, 1)])
    chain.add_block(2, "0xA2", "0xA1", [dep(10, 2)])

    indexer, store = make_indexer(tmp_path, chain)
    indexer.sync_once()

    # Chain resets to a single block with different hash.
    chain.prune_above(0)
    chain.add_block(1, "0xC1", "0xG0", [dep(99, 99)])
    r = indexer.sync_once()
    assert r.reorg is True and r.fork_point == 0
    assert r.detached_blocks == ["0xA2", "0xA1"]
    assert store.list_balances() == {ALICE: 99}
