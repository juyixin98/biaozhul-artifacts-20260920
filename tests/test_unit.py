"""Unit tests for Storage and the reversible reducer (no chain needed)."""
import pytest

from app.reducer import apply_block, revert_block
from app.storage import BlockRow, EventRow, Storage, TIP_HASH, TIP_NUMBER

ALICE = "0x" + "11" * 20
BOB = "0x" + "22" * 20


def ev(number, log_index, name, who, amount, block_hash=None, tx_hash=None, tx_index=0):
    return EventRow(
        block_number=number,
        block_hash=block_hash or f"0x{number:064x}",
        tx_hash=tx_hash or f"0x{number:032x}{log_index:032x}",
        tx_index=tx_index,
        log_index=log_index,
        event_name=name,
        who=who,
        amount=amount,
    )


def test_deposit_withdraw_reducer_roundtrip():
    store = Storage()
    block = [
        ev(1, 0, "Deposited", ALICE, 100),
        ev(1, 1, "Withdrawn", ALICE, 30),
        ev(1, 2, "Deposited", BOB, 50),
    ]
    apply_block(store, block)
    assert store.get_balance(ALICE) == 70
    assert store.get_balance(BOB) == 50
    assert store.get_total("deposited") == 150
    assert store.get_total("withdrawn") == 30

    revert_block(store, block)
    assert store.get_balance(ALICE) == 0
    assert store.get_balance(BOB) == 0
    assert store.get_total("deposited") == 0
    assert store.get_total("withdrawn") == 0


def test_revert_middle_block_leaves_consistent_state():
    store = Storage()
    b1 = [ev(1, 0, "Deposited", ALICE, 100)]
    b2 = [ev(2, 0, "Withdrawn", ALICE, 40), ev(2, 1, "Deposited", BOB, 10)]
    b3 = [ev(3, 0, "Deposited", ALICE, 5)]
    for b in (b1, b2, b3):
        apply_block(store, b)
    assert store.get_balance(ALICE) == 65  # 100 - 40 + 5
    assert store.get_balance(BOB) == 10

    revert_block(store, b3)
    revert_block(store, b2)
    assert store.get_balance(ALICE) == 100
    assert store.get_balance(BOB) == 0
    assert store.get_total("withdrawn") == 0

    # re-applying b2 with different data on the new chain works
    b2_prime = [ev(2, 0, "Deposited", ALICE, 7, block_hash="0x" + "99" * 32)]
    apply_block(store, b2_prime)
    assert store.get_balance(ALICE) == 107


def test_negative_balance_rejected():
    store = Storage()
    with pytest.raises(ValueError):
        apply_block(store, [ev(1, 0, "Withdrawn", ALICE, 1)])


def test_event_unique_key_allows_same_tx_different_block():
    store = Storage()
    # blocks must exist (FK) -- indexer always inserts the block row first
    store.insert_block(BlockRow(5, "0x" + "11" * 32, "0x" + "01" * 32))
    store.insert_block(BlockRow(6, "0x" + "22" * 32, "0x" + "11" * 32))
    common_tx = "0x" + "ab" * 32
    e1 = ev(5, 0, "Deposited", ALICE, 10, block_hash="0x" + "11" * 32, tx_hash=common_tx)
    e2 = ev(6, 0, "Deposited", ALICE, 10, block_hash="0x" + "22" * 32, tx_hash=common_tx)
    assert store.insert_event(e1) is True
    assert store.insert_event(e2) is True  # different block_hash -> distinct rows
    assert store.event_count() == 2
    # exact duplicate ignored
    assert store.insert_event(e1) is False


def test_block_chain_and_meta():
    store = Storage()
    store.insert_block(BlockRow(0, "0xa0", "0x00"))
    store.insert_block(BlockRow(1, "0xa1", "0xa0"))
    store.set_meta(TIP_NUMBER, 1)
    store.set_meta(TIP_HASH, "0xa1")
    assert store.tip().parent_hash == "0xa0"
    store.delete_block(1)
    assert store.get_block(1) is None
