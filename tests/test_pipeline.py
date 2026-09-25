"""多特征流水线与固定桶复用测试。"""
from __future__ import annotations

import numpy as np

from drift.binning import FixedBins
from drift.pipeline import monitor_feature, monitor_features


def test_monitor_features_returns_entry_per_baseline_feature() -> None:
    rng = np.random.default_rng(0)
    baseline = {"a": rng.standard_normal(500), "b": rng.standard_normal(500)}
    current = {"a": rng.standard_normal(200), "b": rng.standard_normal(200)}
    results = monitor_features(baseline, current)
    assert set(results) == {"a", "b"}
    assert all(r.psi >= 0 or np.isinf(r.psi) for r in results.values())


def test_missing_current_feature_treated_as_empty_window() -> None:
    rng = np.random.default_rng(0)
    results = monitor_features(
        {"a": rng.standard_normal(500)}, {"b": rng.standard_normal(10)}
    )
    assert results["a"].current_all_missing
    assert results["a"].n_current == 0


def test_reusing_persisted_bins_matches_fresh_run() -> None:
    # 把 FixedBins 经 dict 序列化后复用，结果应与直接跑一致
    b = np.linspace(-3.0, 3.0, 300)
    c = np.linspace(0.0, 6.0, 120)
    direct = monitor_feature(b, c, n_bins=6)

    bins = FixedBins.from_dict(direct.bins.to_dict())
    reused = monitor_feature(b, c, bins=bins)
    np.testing.assert_allclose(reused.baseline_freq, direct.baseline_freq)
    np.testing.assert_allclose(reused.current_freq, direct.current_freq)
    assert reused.psi == direct.psi
