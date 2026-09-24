"""Unit tests for the FeatureStore."""

import pytest

from pitjoin import FeatureRecord, FeatureStore


def test_ingest_appends_and_validates():
    store = FeatureStore()
    assert store.ingest([FeatureRecord("u1", "f", 1.0, 10, 10)]) == 1
    assert len(store) == 1
    with pytest.raises(ValueError):
        store.ingest([FeatureRecord("", "f", 1.0, 10, 10)])
    with pytest.raises(ValueError):
        store.ingest([FeatureRecord("u1", "f", 1.0, -1, 10)])
    assert len(store) == 1  # failed ingests leave the store unchanged


def test_history_is_append_only():
    store = FeatureStore()
    store.ingest([FeatureRecord("u1", "f", 1.0, 10, 10)])
    store.ingest([FeatureRecord("u1", "f", 2.0, 10, 20)])  # correction, not update
    assert len(store) == 2
    values = sorted(r.value for r in store.records())
    assert values == [1.0, 2.0]


def test_index_rebuilt_after_new_ingest():
    store = FeatureStore()
    store.ingest([FeatureRecord("u1", "f", 1.0, 10, 10)])
    assert store.group("u1", "f").event_ts.tolist() == [10]
    store.ingest([FeatureRecord("u1", "f", 2.0, 5, 6)])
    group = store.group("u1", "f")
    assert group.event_ts.tolist() == [5, 10]  # sorted after rebuild


def test_group_sorted_by_event_then_ingest():
    store = FeatureStore()
    store.ingest([
        FeatureRecord("u1", "f", 3.0, event_ts=10, ingest_ts=30),
        FeatureRecord("u1", "f", 1.0, event_ts=10, ingest_ts=10),
        FeatureRecord("u1", "f", 0.0, event_ts=5, ingest_ts=5),
    ])
    group = store.group("u1", "f")
    assert group.event_ts.tolist() == [5, 10, 10]
    assert group.ingest_ts.tolist() == [5, 10, 30]
    assert group.value.tolist() == [0.0, 1.0, 3.0]


def test_entities_and_features_listing():
    store = FeatureStore()
    store.ingest([
        FeatureRecord("u2", "b", 1.0, 1, 1),
        FeatureRecord("u1", "a", 1.0, 1, 1),
    ])
    assert store.entities() == ["u1", "u2"]
    assert store.features() == ["a", "b"]
