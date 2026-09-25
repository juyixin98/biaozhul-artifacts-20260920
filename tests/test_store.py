"""Tests for the persistent append-only store."""

import json

import pytest

from tl import merkle
from tl.store import LogStore, EntryTooLarge, MAX_ENTRY_BYTES


def test_append_and_reload(tmp_path):
    path = str(tmp_path / "nested" / "log.jsonl")
    store = LogStore(path)
    assert store.size == 0
    assert store.root_at(0) == merkle.EMPTY_TREE_HASH

    payloads = [b"alpha", b"beta", b"", b"gamma\x00delta"]
    for i, p in enumerate(payloads):
        assert store.append(p) == i
    assert store.size == 4
    assert store.entry(2).data == b""

    # reloaded store is identical
    store2 = LogStore(path)
    assert store2.size == 4
    assert [store2.entry(i).data for i in range(4)] == payloads
    assert store2.root_at(4) == store.root_at(4)

    # file is one JSON record per line, append-only
    with open(path) as fh:
        lines = [ln for ln in fh.read().splitlines() if ln]
    assert len(lines) == 4
    rec = json.loads(lines[0])
    assert set(rec) == {"index", "data_b64", "ts"}


def test_historical_roots_are_stable(tmp_path):
    store = LogStore(str(tmp_path / "log.jsonl"))
    roots = {0: store.root_at(0)}
    for i in range(10):
        store.append(f"entry-{i}".encode())
        roots[i + 1] = store.root_at(i + 1)
    store2 = LogStore(str(tmp_path / "log.jsonl"))
    for n, r in roots.items():
        assert store2.root_at(n) == r  # old roots never move


def test_index_out_of_range(tmp_path):
    store = LogStore(str(tmp_path / "log.jsonl"))
    store.append(b"x")
    assert store.entry(1) is None
    with pytest.raises(IndexError):
        store.root_at(2)


def test_entry_too_large(tmp_path):
    store = LogStore(str(tmp_path / "log.jsonl"))
    with pytest.raises(EntryTooLarge):
        store.append(b"x" * (MAX_ENTRY_BYTES + 1))
    assert store.size == 0


def test_corrupt_file_raises(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"index": 0, "data_b64": "AA==", "ts": "t"}\nnot-json\n')
    with pytest.raises(ValueError, match="corrupt"):
        LogStore(str(path))


def test_non_contiguous_index_raises(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(
        '{"index": 0, "data_b64": "AA==", "ts": "t"}\n'
        '{"index": 2, "data_b64": "AA==", "ts": "t"}\n'
    )
    with pytest.raises(ValueError, match="non-contiguous"):
        LogStore(str(path))
