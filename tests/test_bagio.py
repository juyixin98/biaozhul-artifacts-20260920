"""Tests for bag loading, canonical ordering and integrity hashing."""
from __future__ import annotations

import os
import shutil

import pytest

from rosreplay.bagio import BagError, files_match, hash_bag_files, load_bag


def test_load_summary(tmp_bag_root):
    idx = load_bag(tmp_bag_root / "demo")
    assert idx.topics["/tick"].type == "example_interfaces/msg/Int64"
    # 1 second bag: ticks at 0..10 (11), events at 0..20 (21)
    assert idx.topics["/tick"].message_count == 11
    assert idx.topics["/tock"].message_count == 11
    assert idx.topics["/events"].message_count == 21
    assert len(idx) == 11 + 11 + 21


def test_sequences_are_contiguous_and_sorted(index):
    seqs = [e.seq for e in index.entries]
    assert seqs == list(range(len(index)))
    for a, b in zip(index.entries, index.entries[1:]):
        assert (a.timestamp_ns, a.topic, a.order_key) <= (
            b.timestamp_ns,
            b.topic,
            b.order_key,
        )


def test_same_timestamp_topic_order_is_canonical(index):
    """At equal timestamps /tick must precede /tock (regardless of write order).

    The generator physically writes /tock before /tick, so passing proves the
    canonical (timestamp, topic) ordering rather than storage/write order.
    """
    by_ts: dict[int, list[str]] = {}
    for e in index.entries:
        by_ts.setdefault(e.timestamp_ns, []).append(e.topic)
    for ts, topics in by_ts.items():
        paired = [t for t in topics if t in {"/tick", "/tock"}]
        if len(paired) == 2:
            assert paired == ["/tick", "/tock"], (ts, paired)


def test_index_is_stable_across_reloads(tmp_bag_root):
    a = load_bag(tmp_bag_root / "demo")
    b = load_bag(tmp_bag_root / "demo")
    assert [(e.seq, e.topic, e.timestamp_ns) for e in a.entries] == [
        (e.seq, e.topic, e.timestamp_ns) for e in b.entries
    ]


def test_filtered_entries(index):
    only_tick = index.filtered_entries(frozenset({"/tick"}))
    assert {e.topic for e in only_tick} == {"/tick"}
    assert len(only_tick) == 11
    assert index.filtered_entries(None) == index.entries


def test_missing_metadata_raises(tmp_bag_root):
    (tmp_bag_root / "demo" / "metadata.yaml").unlink()
    with pytest.raises(BagError, match="metadata.yaml"):
        load_bag(tmp_bag_root / "demo")


def test_corrupt_data_file_raises(tmp_bag_root):
    mcap = next((tmp_bag_root / "demo").glob("*.mcap"))
    size = os.path.getsize(mcap)
    with open(mcap, "r+b") as fh:
        fh.seek(size // 4)
        fh.write(b"\xff" * (size // 2))
    with pytest.raises(BagError):
        load_bag(tmp_bag_root / "demo")


def test_nonexistent_bag_raises(tmp_bag_root):
    with pytest.raises(BagError):
        load_bag(tmp_bag_root / "does_not_exist")


def test_digest_detects_change(tmp_bag_root):
    idx = load_bag(tmp_bag_root / "demo")
    bag_dir = tmp_bag_root / "demo"
    ok, _ = files_match(idx.files, bag_dir)
    assert ok

    # Modify metadata.yaml -> digest must change.
    meta = bag_dir / "metadata.yaml"
    with open(meta, "a", encoding="utf-8") as fh:
        fh.write("# tampered\n")
    ok, reason = files_match(idx.files, bag_dir)
    assert not ok
    assert "metadata.yaml" in reason


def test_digest_detects_added_and_removed(tmp_bag_root):
    idx = load_bag(tmp_bag_root / "demo")
    bag_dir = tmp_bag_root / "demo"

    added = bag_dir / "extra.txt"
    added.write_text("x")
    ok, reason = files_match(idx.files, bag_dir)
    assert not ok and "added" in reason
    added.unlink()

    some_file = next(p for p in bag_dir.iterdir() if p.suffix == ".mcap")
    stored_name = some_file.name
    some_file.rename(bag_dir / "moved.mcap")
    ok, reason = files_match(idx.files, bag_dir)
    assert not ok
    assert "removed" in reason or stored_name in reason


def test_hash_empty_dir_raises(tmp_path):
    empty = tmp_path / "emptybag"
    empty.mkdir()
    with pytest.raises(BagError):
        hash_bag_files(empty)
