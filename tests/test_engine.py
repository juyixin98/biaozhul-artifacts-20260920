"""引擎测试：以可手算的规范场景逐一核对选择结果与依据。"""
from __future__ import annotations

import numpy as np

from pitjoin.engine import (
    REASON_ALL_FUTURE,
    REASON_ALL_LATE,
    REASON_DUPLICATE,
    REASON_MISSING_KEY,
    REASON_SELECTED,
    Event,
    FeatureRecord,
    FeatureStore,
    JoinConfig,
    pit_join,
)
from pitjoin.synthetic import T0, build_canonical_dataset
from pitjoin.times import hours

H = hours(1)


def _select(store, entity, feature, event_time, *, use_event_as_of=True):
    return store.select(entity, feature, event_time, use_event_as_of)


class TestLateArrivingRevision:
    """晚到特征 / 事后修订不得在入库前被看到。"""

    def test_before_any_revision_uses_old_value(self):
        # Arrange
        ds = build_canonical_dataset()
        # Act
        ev = _select(ds.store, "cust_A", "f_balance", T0 + 12 * H)
        # Assert
        assert ev.reason == REASON_SELECTED
        assert ev.selected_record_id == "rA1"
        assert ev.selected_value == 100.0

    def test_revision_not_visible_before_ingest_time(self):
        # Arrange
        ds = build_canonical_dataset()
        # Act：事件在 T0+1d12h，rA3 生效时间满足但 T0+3d 才入库
        ev = _select(ds.store, "cust_A", "f_balance", T0 + 36 * H)
        # Assert
        assert ev.reason == REASON_SELECTED
        assert ev.selected_record_id == "rA2"
        assert ev.selected_value == 110.0
        late = [e for e in ev.excluded if e.cause == "LATE_INGEST"]
        assert len(late) == 1
        assert late[0].record_id == "rA3"

    def test_revision_visible_after_ingest_time(self):
        # Arrange
        ds = build_canonical_dataset()
        # Act
        ev = _select(ds.store, "cust_A", "f_balance", T0 + 84 * H)
        # Assert
        assert ev.selected_record_id == "rA3"
        assert ev.selected_value == 111.0
        assert ev.candidates_asof == 3

    def test_naive_join_leaks_revision(self):
        # 对照：忽略入库时间时，同事件会错误地拿到晚到修订值
        ds = build_canonical_dataset()
        ev = _select(ds.store, "cust_A", "f_balance", T0 + 36 * H,
                     use_event_as_of=False)
        assert ev.selected_record_id == "rA3"
        assert ev.selected_value == 111.0  # 泄漏：事件时刻尚不可见


class TestSimultaneousVersions:
    """同一生效时刻多个版本的确定性裁决。"""

    def test_higher_version_wins_when_identical_timestamps(self):
        # Arrange
        ds = build_canonical_dataset()
        # Act
        ev = _select(ds.store, "cust_B", "f_score", T0 + 24 * H)
        # Assert
        assert ev.selected_record_id == "rB3"
        assert ev.selected_value == 3.0
        assert ev.tie_resolved is True
        lost = sorted(e.record_id for e in ev.excluded if e.cause == "TIE_LOST")
        assert lost == ["rB1", "rB2"]

    def test_latest_ingest_wins_for_same_version(self):
        # Arrange
        ds = build_canonical_dataset()
        # Act：同 effective、同 version，rB5 入库更晚但在事件前可见
        ev = _select(ds.store, "cust_B", "f_tier", T0 + 24 * H)
        # Assert
        assert ev.selected_record_id == "rB5"
        assert ev.selected_value == 20.0
        lost = [e for e in ev.excluded if e.cause == "TIE_LOST"]
        assert [e.record_id for e in lost] == ["rB4"]

    def test_tie_break_order_is_effective_then_ingest_then_version(self):
        # 更早生效但高版本的记录，不应输给晚生效低版本
        records = [
            FeatureRecord("e1", "f", 1.0, T0, T0, version=9, record_id="old"),
            FeatureRecord("e1", "f", 2.0, T0 + H, T0 + H, version=1, record_id="new"),
        ]
        store = FeatureStore(records)
        ev = _select(store, "e1", "f", T0 + 2 * H)
        assert ev.selected_record_id == "new"  # effective_time 优先级最高


class TestMissingFeatures:
    """缺特征的三种形态。"""

    def test_unknown_entity_is_missing_key(self):
        ds = build_canonical_dataset()
        ev = _select(ds.store, "cust_E", "f_balance", T0 + 24 * H)
        assert ev.reason == REASON_MISSING_KEY
        assert ev.selected_value is None

    def test_only_future_records_is_all_future(self):
        ds = build_canonical_dataset()
        ev = _select(ds.store, "cust_C", "f_balance", T0 + 24 * H)
        assert ev.reason == REASON_ALL_FUTURE
        assert [e.cause for e in ev.excluded] == ["FUTURE_EFFECTIVE"]

    def test_all_records_late_is_all_late(self):
        ds = build_canonical_dataset()
        ev = _select(ds.store, "cust_D", "f_balance", T0 + 24 * H)
        assert ev.reason == REASON_ALL_LATE
        assert [e.cause for e in ev.excluded] == ["LATE_INGEST"]

    def test_missing_values_become_nan_in_frame(self):
        ds = build_canonical_dataset()
        frame = pit_join(ds.events, ds.store,
                         JoinConfig(features=("f_balance",)))
        # cust_E 是最后一个事件
        assert np.isnan(frame.values["f_balance"][-1])


class TestBoundarySemantics:
    """时间边界：等号到底归哪一边，必须固定。"""

    def test_effective_equal_to_event_is_included(self):
        records = [FeatureRecord("e1", "f", 9.0, T0, T0, record_id="r")]
        ev = FeatureStore(records).select("e1", "f", T0, True)
        assert ev.selected_record_id == "r"

    def test_ingest_equal_to_event_is_visible(self):
        records = [FeatureRecord("e1", "f", 9.0, T0, T0, record_id="r")]
        # ingest_time == event_time：恰在提问时刻可见（<= 语义）
        ev = FeatureStore(records).select("e1", "f", T0, True)
        assert ev.reason == REASON_SELECTED

    def test_ingest_one_ms_after_event_is_late(self):
        records = [FeatureRecord("e1", "f", 9.0, T0, T0 + 1, record_id="r")]
        ev = FeatureStore(records).select("e1", "f", T0, True)
        assert ev.reason == REASON_ALL_LATE

    def test_effective_one_ms_after_event_is_future(self):
        records = [FeatureRecord("e1", "f", 9.0, T0 + 1, T0, record_id="r")]
        ev = FeatureStore(records).select("e1", "f", T0, True)
        assert ev.reason == REASON_ALL_FUTURE


class TestBatchJoin:
    def test_design_matrix_shape_and_order(self):
        # Arrange
        ds = build_canonical_dataset()
        # Act
        frame = pit_join(ds.events, ds.store)
        matrix = frame.design_matrix()
        # Assert：3 个特征（f_balance/f_score/f_tier），列顺序固定
        assert matrix.shape == (len(ds.events), 3)
        assert frame.feature_names == ("f_balance", "f_score", "f_tier")

    def test_selection_is_deterministic(self):
        ds = build_canonical_dataset()
        f1 = pit_join(ds.events, ds.store)
        f2 = pit_join(ds.events, ds.store)
        np.testing.assert_array_equal(
            f1.design_matrix(), f2.design_matrix()
        )

    def test_only_requested_features_are_joined(self):
        ds = build_canonical_dataset()
        frame = pit_join(ds.events, ds.store,
                         JoinConfig(features=("f_score",)))
        assert frame.feature_names == ("f_score",)

    def test_events_are_not_cross_joined_between_entities(self):
        # cust_A 的记录绝不能连到 cust_B
        ds = build_canonical_dataset()
        frame = pit_join(
            (Event("cust_B", T0 + 24 * H),), ds.store,
            JoinConfig(features=("f_balance",)),
        )
        assert np.isnan(frame.values["f_balance"][0])  # B 无 f_balance

    def test_exact_duplicate_records_are_flagged(self):
        # Arrange：键完全相同的重复记录
        records = [
            FeatureRecord("e1", "f", 5.0, T0, T0, version=1, record_id="dup-a"),
            FeatureRecord("e1", "f", 5.0, T0, T0, version=1, record_id="dup-b"),
        ]
        # Act
        ev = FeatureStore(records).select("e1", "f", T0 + H, True)
        # Assert：稳定取首条，原因标记 DUPLICATE
        assert ev.reason == REASON_DUPLICATE
        assert ev.selected_record_id == "dup-a"
        assert [e.cause for e in ev.excluded] == ["DUPLICATE"]
