"""Unit tests for the point-in-time join core semantics."""

import numpy as np
import pytest

from pitjoin import FeatureRecord, FeatureStore, SpineRow, join_as_matrix, point_in_time_join
from pitjoin.join import REASON_ALL_FUTURE, REASON_ALL_LATE, REASON_NO_RECORDS
from pitjoin.reference import naive_join


def make_store(records):
    store = FeatureStore()
    store.ingest(records)
    return store


class TestLateArrival:
    def test_late_revision_excluded(self):
        store = make_store([
            FeatureRecord("u1", "f", 0.20, event_ts=200, ingest_ts=215),
            FeatureRecord("u1", "f", 0.99, event_ts=200, ingest_ts=260),  # late revision
        ])
        [r] = point_in_time_join(store, [SpineRow("u1", 250)], ["f"])
        assert r["value"] == 0.20
        assert r["selected_ingest_ts"] == 215
        assert r["rejected_late_ingest"] == 1

    def test_late_revision_used_once_visible(self):
        store = make_store([
            FeatureRecord("u1", "f", 0.20, event_ts=200, ingest_ts=215),
            FeatureRecord("u1", "f", 0.99, event_ts=200, ingest_ts=260),
        ])
        [r] = point_in_time_join(store, [SpineRow("u1", 300)], ["f"])
        assert r["value"] == 0.99
        assert r["selected_ingest_ts"] == 260

    def test_late_arriving_fact_excluded(self):
        store = make_store([
            FeatureRecord("u1", "f", 0.10, event_ts=100, ingest_ts=110),
            FeatureRecord("u1", "f", 0.30, event_ts=300, ingest_ts=400),  # late arrival
        ])
        [r] = point_in_time_join(store, [SpineRow("u1", 350)], ["f"])
        assert r["value"] == 0.10  # event 300 not yet ingested at 350
        assert r["rejected_future_event"] == 0  # event 300 <= 350 exists
        assert r["rejected_late_ingest"] == 1  # but its ingest 400 > 350


class TestSameEventMultiVersion:
    def test_tie_break_by_latest_ingest(self):
        store = make_store([
            FeatureRecord("u2", "f", 0.50, event_ts=100, ingest_ts=105),
            FeatureRecord("u2", "f", 0.55, event_ts=100, ingest_ts=120),
        ])
        [r] = point_in_time_join(store, [SpineRow("u2", 130)], ["f"])
        assert r["value"] == 0.55
        assert r["selected_event_ts"] == 100
        assert r["selected_ingest_ts"] == 120

    def test_correction_not_visible_before_its_ingest(self):
        store = make_store([
            FeatureRecord("u2", "f", 0.50, event_ts=100, ingest_ts=105),
            FeatureRecord("u2", "f", 0.55, event_ts=100, ingest_ts=120),
        ])
        [r] = point_in_time_join(store, [SpineRow("u2", 110)], ["f"])
        assert r["value"] == 0.50


class TestMissing:
    def test_no_records_for_feature(self):
        store = make_store([FeatureRecord("u1", "f", 1.0, event_ts=10, ingest_ts=10)])
        [r] = point_in_time_join(store, [SpineRow("u1", 100)], ["other"])
        assert r["value"] is None
        assert r["reason"] == REASON_NO_RECORDS

    def test_unknown_entity(self):
        store = make_store([FeatureRecord("u1", "f", 1.0, event_ts=10, ingest_ts=10)])
        [r] = point_in_time_join(store, [SpineRow("ghost", 100)], ["f"])
        assert r["value"] is None
        assert r["reason"] == REASON_NO_RECORDS

    def test_all_versions_in_future(self):
        store = make_store([FeatureRecord("u1", "f", 1.0, event_ts=500, ingest_ts=500)])
        [r] = point_in_time_join(store, [SpineRow("u1", 100)], ["f"])
        assert r["value"] is None
        assert r["reason"] == REASON_ALL_FUTURE

    def test_all_versions_late_ingest(self):
        store = make_store([FeatureRecord("u1", "f", 1.0, event_ts=50, ingest_ts=500)])
        [r] = point_in_time_join(store, [SpineRow("u1", 100)], ["f"])
        assert r["value"] is None
        assert r["reason"] == REASON_ALL_LATE


class TestBoundaries:
    def test_event_ts_equal_spine_is_visible(self):
        store = make_store([FeatureRecord("u1", "f", 7.0, event_ts=100, ingest_ts=50)])
        [r] = point_in_time_join(store, [SpineRow("u1", 100)], ["f"])
        assert r["value"] == 7.0

    def test_ingest_ts_equal_spine_is_visible(self):
        store = make_store([FeatureRecord("u1", "f", 7.0, event_ts=50, ingest_ts=100)])
        [r] = point_in_time_join(store, [SpineRow("u1", 100)], ["f"])
        assert r["value"] == 7.0

    def test_ingest_one_after_spine_is_hidden(self):
        store = make_store([FeatureRecord("u1", "f", 7.0, event_ts=50, ingest_ts=101)])
        [r] = point_in_time_join(store, [SpineRow("u1", 100)], ["f"])
        assert r["value"] is None


class TestCrossValidation:
    """The NumPy join must agree with the naive reference on random data."""

    def test_matches_naive_reference(self):
        rng = np.random.default_rng(42)
        entities = [f"u{i}" for i in range(8)]
        features = ["f1", "f2", "f3"]
        records = [
            FeatureRecord(
                entity_id=str(rng.choice(entities)),
                feature=str(rng.choice(features)),
                value=float(rng.normal()),
                event_ts=int(rng.integers(0, 500)),
                ingest_ts=int(rng.integers(0, 600)),
            )
            for _ in range(400)
        ]
        spine = [
            SpineRow(str(rng.choice(entities)), int(rng.integers(0, 600)))
            for _ in range(120)
        ]
        store = make_store(records)
        got = point_in_time_join(store, spine, features)
        expected = naive_join(records, spine, features)

        assert len(got) == len(spine) * len(features)
        for i, r in enumerate(got):
            ref = expected[i // len(features)][i % len(features)]
            if ref is None:
                assert r["value"] is None, r
            else:
                assert r["value"] == ref.value
                assert r["selected_event_ts"] == ref.event_ts
                assert r["selected_ingest_ts"] == ref.ingest_ts


class TestMatrixView:
    def test_matrix_and_missing_mask(self):
        store = make_store([
            FeatureRecord("u1", "f1", 1.5, event_ts=10, ingest_ts=10),
            FeatureRecord("u1", "f2", 2.5, event_ts=10, ingest_ts=10),
        ])
        spine = [SpineRow("u1", 20), SpineRow("u1", 5)]
        matrix, missing, details = join_as_matrix(store, spine, ["f1", "f2"])
        assert matrix.shape == (2, 2)
        assert not missing[0].any()
        assert missing[1].all()  # spine ts 5 predates every record
        assert np.isnan(matrix[1]).all()
        assert matrix[0].tolist() == [1.5, 2.5]
        assert len(details) == 4

    def test_empty_features_rejected(self):
        store = make_store([])
        with pytest.raises(ValueError):
            point_in_time_join(store, [SpineRow("u1", 1)], [])
