"""Tests for the versioned SQLite evidence store."""
import pytest

from app.provenance.store import EvidenceStore, InputRow, RelationData, VersionNotFound


def _populate(store: EvidenceStore) -> int:
    version_id = store.create_version("unit-set")
    store.add_relation(
        version_id,
        "R",
        ("id", "b"),
        [
            InputRow("r1", ("a", 1), weight=2.0),
            InputRow("r2", ("a", None), weight=3.0),  # NULL is preserved
        ],
    )
    store.add_relation(
        version_id,
        "S",
        ("b", "c"),
        [InputRow("s1", (1, "x"), weight=5.0)],
    )
    return version_id


def test_round_trip_relation_data(tmp_path):
    store = EvidenceStore(tmp_path / "ev.db")
    version_id = _populate(store)

    r = store.fetch_relation(version_id, "R")
    assert isinstance(r, RelationData)
    assert r.columns == ("id", "b")
    assert r.rows[0].row_id == "r1"
    assert r.rows[0].values == ("a", 1)
    assert r.rows[0].weight == 2.0
    # NULL survives storage
    assert r.rows[1].values == ("a", None)

    s = store.fetch_relation(version_id, "S")
    assert s.rows[0].values == (1, "x")


def test_versions_are_isolated(tmp_path):
    store = EvidenceStore(tmp_path / "ev.db")
    v1 = _populate(store)
    v2 = store.create_version("second-set")
    store.add_relation(v2, "R", ("id", "b"), [InputRow("r9", ("z", 9))])

    # reading R at v1 must never see v2 rows
    v1_rows = {row.row_id for row in store.fetch_relation(v1, "R").rows}
    assert v1_rows == {"r1", "r2"}
    assert store.fetch_relation(v2, "R").rows[0].row_id == "r9"
    assert store.latest_version() == v2


def test_missing_version_and_relation_are_named_errors(tmp_path):
    store = EvidenceStore(tmp_path / "ev.db")
    with pytest.raises(VersionNotFound):
        store.fetch_relation(999, "R")
    vid = store.create_version("x")
    with pytest.raises(KeyError):
        store.fetch_relation(vid, "ghost")


def test_duplicate_row_id_rejected(tmp_path):
    store = EvidenceStore(tmp_path / "ev.db")
    vid = store.create_version("x")
    store.add_relation(vid, "R", ("a",), [InputRow("r1", (1,))])
    with pytest.raises(Exception):
        store.add_relation(vid, "R", ("a",), [InputRow("r1", (2,))])


def test_list_relations_and_schema(tmp_path):
    store = EvidenceStore(tmp_path / "ev.db")
    vid = _populate(store)
    assert set(store.list_relations(vid)) == {"R", "S"}
    schema = store.fetch_schema(vid)
    assert schema["R"] == ("id", "b")
    assert schema["S"] == ("b", "c")
