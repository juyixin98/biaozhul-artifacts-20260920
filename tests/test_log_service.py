"""Integration tests for TransparentLog service semantics (no HTTP)."""

import pytest

from tl.log import TransparentLog
from tl.signing import KeyManager, verify_sth_signature
from tl.store import LogStore
from tl import merkle


@pytest.fixture
def tlog(tmp_path):
    store = LogStore(str(tmp_path / "log.jsonl"))
    keys = KeyManager(str(tmp_path / "key.bin"))
    return TransparentLog(store, keys)


def test_append_inclusion_consistency_end_to_end(tlog):
    for i in range(10):
        tlog.add(f"record-{i}".encode())
    assert tlog.size() == 10

    sth = tlog.current_sth()
    assert sth.tree_size == 10
    ok, _ = verify_sth_signature(
        tlog.keys.public_key_raw(),
        sth.tree_size, sth.root_hash, sth.timestamp_us, sth.signature,
    )
    assert ok

    for idx in range(10):
        res = tlog.inclusion_by_index(idx)
        assert res.leaf_hash == merkle.leaf_hash(f"record-{idx}".encode())
        assert merkle.verify_inclusion(
            res.leaf_index, res.tree_size, res.leaf_hash,
            res.proof, sth.root_hash,
        )


def test_historical_inclusion_and_consistency(tlog):
    for i in range(20):
        tlog.add(bytes([i]))
    # client captured an STH at size 13 (non-power-of-two), later tree is 20
    old = tlog.sth_at_size(13)
    later = tlog.current_sth()
    assert later.tree_size == 20

    # inclusion proof for a leaf against the historical head
    inc = tlog.inclusion_by_index(5, tree_size=13)
    assert inc.tree_size == 13 and inc.root_hash == old.root_hash
    assert merkle.verify_inclusion(
        inc.leaf_index, 13, inc.leaf_hash, inc.proof, old.root_hash
    )

    # consistency between the two heads
    con = tlog.consistency(13, 20)
    assert merkle.verify_consistency(
        13, 20, old.root_hash, later.root_hash, con.proof
    )


def test_invalid_indexes_rejected(tlog):
    for i in range(5):
        tlog.add(b"x")
    with pytest.raises(IndexError):
        tlog.inclusion_by_index(5)          # idx == size
    with pytest.raises(IndexError):
        tlog.inclusion_by_index(0, 6)       # future size
    with pytest.raises(IndexError):
        tlog.inclusion_by_index(-1)
    with pytest.raises(IndexError):
        tlog.consistency(6, 5)
    with pytest.raises(IndexError):
        tlog.sth_at_size(6)


def test_signed_sth_for_forged_root_fails(tlog):
    tlog.add(b"a")
    sth = tlog.current_sth()
    ok, _ = verify_sth_signature(
        tlog.keys.public_key_raw(),
        sth.tree_size, b"\x00" * 32, sth.timestamp_us, sth.signature,
    )
    assert not ok


def test_append_only_old_roots_remain_valid(tlog, tmp_path):
    for i in range(7):
        tlog.add(bytes([i]))
    root7 = tlog.store.root_at(7)
    for i in range(8, 20):
        tlog.add(bytes([i]))
    assert tlog.store.root_at(7) == root7
    con = tlog.consistency(7, 19)
    assert merkle.verify_consistency(
        7, 19, root7, tlog.store.root_at(19), con.proof
    )
