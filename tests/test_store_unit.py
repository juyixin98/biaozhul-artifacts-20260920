"""Unit tests for reversible state transitions in IndexStore (no chain)."""

from __future__ import annotations

import pytest

from app.store import IndexStore


def ev(name, account, amount, tag, log_index, tx_hash=None, to_account=None):
    return {
        "name": name,
        "account": account,
        "to_account": to_account,
        "amount": amount,
        "tag": tag,
        "log_index": log_index,
        "tx_hash": tx_hash or f"0xtx{tag}",
        "block_hash": None,  # filled by caller/ingest
        "block_number": 0,
    }


ALICE = "0xAA00000000000000000000000000000000000001"
BOB = "0xBB00000000000000000000000000000000000002"


def make_store(tmp_path):
    return IndexStore(tmp_path / "u.db")


def test_ingest_then_full_rollback_restores_zero(tmp_path):
    s = make_store(tmp_path)
    s.ingest_block(
        1, "0xh1", "0xh0",
        [ev("Deposited", ALICE, 100, 1, 0), ev("Deposited", BOB, 50, 2, 1)],
    )
    s.ingest_block(
        2, "0xh2", "0xh1",
        [
            ev("Transferred", ALICE, 30, 3, 0, to_account=BOB),
            ev("Withdrawn", BOB, 20, 4, 1),
        ],
    )
    assert s.list_balances() == {ALICE: 70, BOB: 60}

    s.detach_block("0xh2")
    assert s.list_balances() == {ALICE: 100, BOB: 50}
    s.detach_block("0xh1")
    # Zero accounts remain as rows with value 0.
    assert s.list_balances() == {ALICE: 0, BOB: 0}
    assert s.list_events() == []


def test_orphan_headers_kept_but_events_removed(tmp_path):
    s = make_store(tmp_path)
    s.ingest_block(1, "0xh1", "0xh0", [ev("Deposited", ALICE, 100, 1, 0)])
    s.detach_block("0xh1")
    orphans = s.list_orphan_blocks()
    assert [b["hash"] for b in orphans] == ["0xh1"]
    assert s.list_events(canonical_only=False) == []


def test_readopt_block_reapplies_effects(tmp_path):
    s = make_store(tmp_path)
    s.ingest_block(1, "0xh1", "0xh0", [ev("Deposited", ALICE, 100, 1, 0)])
    s.ingest_block(2, "0xh2", "0xh1", [ev("Deposited", BOB, 50, 2, 0)])
    # h2 gets orphaned by a reorg; its event rows are deleted, header retained.
    s.detach_block("0xh2")
    assert s.list_balances() == {ALICE: 100, BOB: 0}

    # The previously detached block becomes canonical again (reorg reversal).
    s.readopt_block(2, "0xh2", "0xh1", [ev("Deposited", BOB, 50, 2, 0)])
    assert s.list_balances() == {ALICE: 100, BOB: 50}
    assert {e["tag"] for e in s.list_events()} == {"1", "2"}


def test_same_tx_in_two_blocks_disambiguated_by_block_hash(tmp_path):
    s = make_store(tmp_path)
    e_a = ev("Deposited", ALICE, 333, 3331, 0, tx_hash="0xsame")
    e_b = dict(e_a)
    s.ingest_block(3, "0xA3", "0xh2", [e_a])
    s.ingest_block(3, "0xB3", "0xh2", [e_b])
    rows = s.list_events(canonical_only=False)
    assert len(rows) == 2
    assert {r["block_hash"] for r in rows} == {"0xA3", "0xB3"}
    # Canonical fold counts only one (both were marked canonical here, so
    # detach one and confirm the other remains).
    s.detach_block("0xA3")
    canonical = s.list_events()
    assert len(canonical) == 1 and canonical[0]["block_hash"] == "0xB3"
    assert s.list_balances() == {ALICE: 333}


def test_rollback_negative_balance_is_an_error(tmp_path):
    s = make_store(tmp_path)
    # Undoing a Deposit while the balance is below its amount must fail the
    # inverse invariant rather than silently underflowing. (The engine always
    # detaches tip-first, so this guards an out-of-order direct call.)
    s.ingest_block(1, "0xh1", "0xh0", [ev("Deposited", ALICE, 10, 1, 0)])
    s.ingest_block(2, "0xh2", "0xh1", [ev("Withdrawn", ALICE, 10, 2, 0)])
    assert s.list_balances() == {ALICE: 0}
    with pytest.raises(ValueError, match="rollback invariant"):
        s.detach_block("0xh1")  # balance 0, inverse deposit would be -10
