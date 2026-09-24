"""Bag indexing, deterministic ordering and corruption detection."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import requires_ros


@requires_ros
def test_index_sequential_and_hashes(good_bag, tmp_path):
    from app import bagstore

    index, payloads = bagstore.index_bag_with_payloads(Path(good_bag["uri"]))
    n = len(good_bag["messages"])
    assert len(index.entries) == n
    assert len(payloads) == n

    # stable seq is 1..N
    assert [e.seq for e in index.entries] == list(range(1, n + 1))

    # timestamps non-decreasing and match source
    ts = [e.timestamp_ns for e in index.entries]
    assert ts == sorted(ts)

    # data hash really matches payload
    import hashlib

    for entry in index.entries:
        assert hashlib.sha256(payloads[entry.seq]).hexdigest() == entry.data_sha256
        assert entry.data_length == len(payloads[entry.seq])


@requires_ros
def test_same_timestamp_tie_break_is_deterministic(good_bag):
    from app import bagstore

    index1, _ = bagstore.index_bag_with_payloads(Path(good_bag["uri"]))
    index2, _ = bagstore.index_bag_with_payloads(Path(good_bag["uri"]))

    # exact same ordering across two independent index runs
    order1 = [(e.timestamp_ns, e.topic, e.data_sha256) for e in index1.entries]
    order2 = [(e.timestamp_ns, e.topic, e.data_sha256) for e in index2.entries]
    assert order1 == order2

    # find the groups produced by same_time_groups and check topic ordering
    same_ts_groups = {}
    for e in index1.entries:
        same_ts_groups.setdefault(e.timestamp_ns, []).append(e.topic)
    groups = [topics for topics in same_ts_groups.values() if len(topics) > 1]
    assert groups, "fixture should have produced shared timestamps"
    for topics in groups:
        assert topics == sorted(topics)


@requires_ros
def test_index_hash_stable_but_changes_with_content(tmp_path):
    from app import bagstore
    from bagtools.bagmaker import make_bag

    d1 = tmp_path / "a"; d2 = tmp_path / "b"
    make_bag(d1, messages=6, same_time_groups=0)
    make_bag(d2, messages=6, same_time_groups=0)
    h1 = bagstore.index_bag(d1).message_index_sha256
    h2 = bagstore.index_bag(d2).message_index_sha256
    assert h1 == h2  # identical contents/dir -> identical hash

    # modify payload: rebuild a bag with different content into c
    d3 = tmp_path / "c"
    make_bag(d3, messages=6, same_time_groups=0, start_ns=999)
    h3 = bagstore.index_bag(d3).message_index_sha256
    assert h3 != h1


@requires_ros
@pytest.mark.parametrize(
    "mode",
    [
        "truncate_storage",
        "garbage_storage",
        "missing_metadata",
        "bad_metadata",
        "missing_storage",
    ],
)
def test_corrupt_bags_rejected(tmp_path, mode):
    from app import bagstore
    from bagtools.bagmaker import make_corrupt

    bag_dir = tmp_path / f"bad_{mode}"
    make_corrupt(bag_dir, mode, messages=8)
    with pytest.raises(bagstore.CorruptBagError):
        bagstore.index_bag(bag_dir)


@requires_ros
def test_extra_file_detected_as_source_change(tmp_path):
    from app import bagstore
    from bagtools.bagmaker import make_bag, make_corrupt

    bag_dir = tmp_path / "extra"
    make_bag(bag_dir, messages=6)
    index = bagstore.index_bag(bag_dir)
    # Add rogue file directly.
    (Path(bag_dir) / "rogue_data_9.mcap").write_bytes(b"xxxx")
    with pytest.raises(bagstore.CorruptBagError):
        bagstore.verify_index_fresh(index)


@requires_ros
def test_modified_storage_file_detected(tmp_path):
    from app import bagstore
    from bagtools.bagmaker import make_bag

    bag_dir = tmp_path / "mod"
    make_bag(bag_dir, messages=8)
    index = bagstore.index_bag(bag_dir)
    # Flip one byte in the storage file (physical tampering).
    mcap = next(bag_dir.glob("*.mcap"))
    with mcap.open("r+b") as fh:
        fh.seek(mcap.stat().st_size - 5)
        b = fh.read(1)
        fh.seek(mcap.stat().st_size - 5)
        fh.write(bytes([b[0] ^ 0x01]))
    with pytest.raises(bagstore.CorruptBagError):
        bagstore.verify_index_fresh(index)


@requires_ros
def test_resolve_uri_enforces_roots(tmp_path):
    from app import bagstore
    from bagtools.bagmaker import make_bag

    inside = tmp_path / "allowed" / "bag"
    make_bag(inside, messages=4)
    outside = tmp_path / "secret" / "bag"
    make_bag(outside, messages=4)

    resolved = bagstore.resolve_bag_uri(str(inside), [tmp_path / "allowed"])
    assert resolved == inside.resolve()
    with pytest.raises(bagstore.BagAccessError):
        bagstore.resolve_bag_uri(str(outside), [tmp_path / "allowed"])
