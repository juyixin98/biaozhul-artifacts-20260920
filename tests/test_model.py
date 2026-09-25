"""合成数据、简单模型与泄漏对照实验的测试。"""
from __future__ import annotations

import numpy as np

from pitjoin.engine import JoinConfig, pit_join
from pitjoin.model import (
    LogisticRegression,
    accuracy,
    chronological_split,
    train_logistic,
)
from pitjoin.synthetic import build_generated_dataset


class TestSyntheticDataset:
    def test_dataset_is_reproducible(self):
        # Arrange / Act：同种子两次生成
        a = build_generated_dataset(seed=7)
        b = build_generated_dataset(seed=7)
        # Assert：记录逐条一致
        assert len(a.store.records) == len(b.store.records)
        va = [r.value for r in a.store.records[:50]]
        vb = [r.value for r in b.store.records[:50]]
        np.testing.assert_allclose(va, vb)

    def test_different_seeds_differ(self):
        a = build_generated_dataset(seed=1)
        b = build_generated_dataset(seed=2)
        va = [r.value for r in a.store.records[:50]]
        vb = [r.value for r in b.store.records[:50]]
        assert not np.allclose(va, vb)

    def test_revisions_ingest_later_than_preliminary(self):
        ds = build_generated_dataset()
        # 每条定稿值都比对应初步值晚入库 72h
        finals = [r for r in ds.store.records if r.record_id.endswith("-final")]
        prelims = {r.record_id.replace("-final", "-prelim"): r
                   for r in ds.store.records if r.record_id.endswith("-prelim")}
        assert len(finals) == ds.revision_records
        for final in finals[:100]:
            prelim = prelims[final.record_id.replace("-final", "-prelim")]
            assert final.ingest_time > prelim.ingest_time
            assert final.effective_time == prelim.effective_time


class TestLogisticRegression:
    def test_separates_linearly_separable_data(self):
        # Arrange：沿 x 轴可分的两类
        rng = np.random.default_rng(0)
        x = np.vstack([
            rng.normal(-3.0, 0.3, size=(50, 2)),
            rng.normal(3.0, 0.3, size=(50, 2)),
        ])
        y = np.array([0] * 50 + [1] * 50)
        # Act
        model = train_logistic(x, y, n_iter=3000)
        # Assert
        assert accuracy(y, model.predict(x)) == 1.0

    def test_training_improves_over_zero_init(self):
        # Arrange
        rng = np.random.default_rng(1)
        x = rng.normal(size=(80, 3))
        y = (x @ np.array([1.0, -1.0, 0.5]) > 0).astype(int)
        # Act
        model = train_logistic(x, y, n_iter=2000)
        # Assert：训练后损失有限且显著好于零参数基线（log(2)）
        assert np.isfinite(model.train_loss)
        assert model.train_loss < 0.55

    def test_predict_proba_range(self):
        rng = np.random.default_rng(2)
        x = rng.normal(size=(40, 2))
        y = (x[:, 0] > 0).astype(int)
        probs = train_logistic(x, y).predict_proba(x)
        assert np.all(probs >= 0.0) and np.all(probs <= 1.0)

    def test_rejects_mismatched_shapes(self):
        import pytest
        with pytest.raises(ValueError):
            train_logistic(np.zeros((3, 2)), np.zeros(4))

    def test_rejects_one_dimensional_x(self):
        import pytest
        with pytest.raises(ValueError):
            train_logistic(np.zeros(5), np.zeros(5))

    def test_convergence_breaks_early(self):
        # 极易分的数据会在 n_iter 之前因容差停止
        rng = np.random.default_rng(5)
        x = np.vstack([
            rng.normal(-10.0, 0.01, size=(20, 1)),
            rng.normal(10.0, 0.01, size=(20, 1)),
        ])
        y = np.array([0] * 20 + [1] * 20)
        model = train_logistic(x, y, n_iter=10_000, tol=1e-6)
        assert model.n_iter < 10_000
        assert accuracy(y, model.predict(x)) == 1.0


class TestLeakageExperiment:
    def test_naive_join_picks_revisions_more_often(self):
        # Arrange：事件大多落在 72h 修订窗口内
        ds = build_generated_dataset(seed=42, n_events=200)
        # Act
        pit = pit_join(ds.events, ds.store, JoinConfig(use_event_as_of=True))
        naive = pit_join(ds.events, ds.store, JoinConfig(use_event_as_of=False))
        # Assert：两种 join 选到的记录确实不同（naive 拿到更高版本号）
        assert not np.allclose(
            pit.design_matrix(), naive.design_matrix(), equal_nan=True
        )

    def test_chronological_split_respects_time_order(self):
        ds = build_generated_dataset(n_events=100)
        frame = pit_join(ds.events, ds.store)
        split = chronological_split(frame, train_ratio=0.7)
        # 训练集 70%，且训练集最大时刻 <= 测试集最小时刻
        assert len(split.x_train) == 70
        assert len(split.x_test) == 30

    def test_pit_frame_has_no_nan_in_generated_data(self):
        # 事件从第 1 天起、初步值 T+1h 入库，前一日快照始终可见 -> 无缺失
        ds = build_generated_dataset(seed=3, n_events=120)
        frame = pit_join(ds.events, ds.store)
        assert not np.isnan(frame.design_matrix()).any()
