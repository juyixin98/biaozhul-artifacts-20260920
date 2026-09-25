"""PSI / 平滑规则 / 其他指标的单元测试。"""
from __future__ import annotations

import numpy as np
import pytest

from drift.binning import assign_counts, fit_fixed_bins
from drift.metrics import (
    DEFAULT_ALPHA,
    DriftResult,
    compute_drift,
    interpret_psi,
    js_divergence,
    psi,
    smooth_distribution,
    total_variation,
)
from drift.pipeline import monitor_feature
from drift import synthetic


# ---------- 平滑规则 ----------

def test_no_smoothing_keeps_zeros() -> None:
    p, q = smooth_distribution(
        np.array([5, 5, 0]), np.array([0, 6, 4]), method="none"
    )
    np.testing.assert_allclose(p, [0.5, 0.5, 0.0])
    np.testing.assert_allclose(q, [0.0, 0.6, 0.4])


def test_laplace_smoothing_sums_to_one_and_fills_empty() -> None:
    p, q = smooth_distribution(
        np.array([10, 0, 0]), np.array([0, 0, 10]),
        method="laplace", alpha=0.5,
    )
    assert p.shape == q.shape
    assert np.all(p > 0) and np.all(q > 0)
    np.testing.assert_allclose(p.sum(), 1.0)
    np.testing.assert_allclose(q.sum(), 1.0)
    # 对称伪计数：原始比例不应被破坏（10 的桶仍最大）
    assert p[0] == p.max() and q[2] == q.max()


def test_floor_smoothing_renormalizes() -> None:
    p, q = smooth_distribution(
        np.array([10, 0]), np.array([0, 10]), method="floor", epsilon=0.01
    )
    np.testing.assert_allclose(p.sum(), 1.0)
    np.testing.assert_allclose(q.sum(), 1.0)
    # 抬底后重新归一化：所有桶严格为正（最终下限会略低于 epsilon）
    assert np.all(p > 0.0)
    assert np.all(q > 0.0)


def test_smoothing_validation() -> None:
    with pytest.raises(ValueError, match="alpha"):
        smooth_distribution(np.array([1]), np.array([1]), method="laplace", alpha=0)
    with pytest.raises(ValueError, match="epsilon"):
        smooth_distribution(np.array([1]), np.array([1]), method="floor", epsilon=0)
    with pytest.raises(ValueError, match="未知平滑"):
        smooth_distribution(np.array([1]), np.array([1]), method="bogus")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="不一致"):
        smooth_distribution(np.array([1, 2]), np.array([1]))


def test_zero_total_without_smoothing_returns_zeros() -> None:
    p, q = smooth_distribution(
        np.zeros(3), np.array([1.0, 2.0, 1.0]), method="none"
    )
    np.testing.assert_array_equal(p, np.zeros(3))


# ---------- PSI 数学性质 ----------

def test_psi_zero_for_identical_distributions() -> None:
    p = np.array([0.1, 0.4, 0.3, 0.2])
    value, per_bin = psi(p, p)
    assert value == pytest.approx(0.0, abs=1e-12)
    np.testing.assert_allclose(per_bin, 0.0, atol=1e-12)


def test_psi_symmetric_under_swap_for_full_support() -> None:
    p = np.array([0.2, 0.3, 0.5])
    q = np.array([0.5, 0.2, 0.3])
    v1, _ = psi(p, q)
    v2, _ = psi(q, p)
    assert v1 == pytest.approx(v2)


def test_psi_handcrafted_two_bin_value() -> None:
    # p=(0.8,0.2), q=(0.5,0.5) 的手算值
    p = np.array([0.8, 0.2])
    q = np.array([0.5, 0.5])
    expected = (0.5 - 0.8) * np.log(0.5 / 0.8) + (0.5 - 0.2) * np.log(
        0.5 / 0.2
    )
    value, _ = psi(p, q)
    assert value == pytest.approx(expected)


def test_psi_is_infinite_when_one_side_has_empty_bin() -> None:
    value, per_bin = psi(np.array([1.0, 0.0]), np.array([0.5, 0.5]))
    assert np.isinf(value)
    assert np.isinf(per_bin).sum() == 1  # 仅桶 1 一侧为 0


def test_psi_both_zero_bin_contributes_nothing() -> None:
    value, per_bin = psi(np.array([1.0, 0.0]), np.array([1.0, 0.0]))
    assert value == pytest.approx(0.0)
    assert per_bin[1] == 0.0


# ---------- JS / TVD ----------

def test_js_zero_for_identical_and_bounded_for_different() -> None:
    p = np.array([0.2, 0.3, 0.5])
    assert js_divergence(p, p) == pytest.approx(0.0, abs=1e-12)
    value = js_divergence(np.array([1.0, 0.0]), np.array([0.0, 1.0]))
    assert value == pytest.approx(1.0)  # base=2 时支撑完全不相交 -> 1


def test_tvd_basic() -> None:
    assert total_variation(np.array([0.5, 0.5]), np.array([0.2, 0.8])) == pytest.approx(0.3)


def test_js_tvd_undefined_when_one_side_all_zero() -> None:
    assert np.isnan(js_divergence(np.zeros(2), np.array([0.5, 0.5])))
    assert np.isnan(total_variation(np.zeros(2), np.array([0.5, 0.5])))


# ---------- interpret_psi 措辞 ----------

def test_interpret_psi_uses_rule_of_thumb_wording() -> None:
    assert interpret_psi(0.05) == "little_drift_rule_of_thumb"
    assert interpret_psi(0.15) == "some_drift_rule_of_thumb"
    assert interpret_psi(0.3) == "severe_drift_rule_of_thumb"
    assert interpret_psi(float("inf")) == "severe_drift_rule_of_thumb"
    assert interpret_psi(float("nan")) == "undefined"


# ---------- compute_drift 端到端 ----------

def _counts(baseline, current, n_bins=10):
    bins = fit_fixed_bins(baseline, n_bins=n_bins)
    return assign_counts(bins, baseline), assign_counts(bins, current)


def test_same_distribution_has_small_psi() -> None:
    b, c = synthetic.same_distribution(seed=42)
    result = monitor_feature(b, c, feature="same")
    assert result.psi < 0.1
    assert result.js_divergence < 0.05
    assert result.tvd < 0.2
    assert result.to_dict()["psi_band"] == "little_drift_rule_of_thumb"
    assert not result.small_sample


def test_shifted_distribution_has_large_psi() -> None:
    b, c = synthetic.shifted_distribution(mean_shift=1.0, seed=42)
    same, shifted = (
        monitor_feature(b, synthetic.same_distribution(seed=42)[1]),
        monitor_feature(b, c, feature="shifted"),
    )
    assert shifted.psi > same.psi * 5
    assert shifted.psi > 0.25
    assert shifted.missing_rate_delta == pytest.approx(0.0)


def test_none_smoothing_is_finite_when_all_bins_supported_by_both() -> None:
    # 固定 min/max 建箱时，基线两侧溢出桶结构上恒为 0；因此“none 平滑得到
    # 有限 PSI”的前提是双方在每个桶（含溢出桶）都有计数。手工构造此场景：
    from drift.binning import BinCounts

    bins = fit_fixed_bins(np.arange(0.0, 11.0), n_bins=5)  # 8 个桶
    base = BinCounts(bins, np.array([2, 20, 30, 30, 20, 10, 2, 0]))
    cur = BinCounts(bins, np.array([5, 10, 20, 25, 25, 20, 8, 0]))
    result = compute_drift(base, cur, smoothing="none")
    assert np.isfinite(result.psi)
    assert result.psi > 0
    np.testing.assert_allclose(result.baseline_freq.sum(), 1.0)


def test_none_smoothing_is_infinite_when_current_exceeds_baseline_range() -> None:
    # 基线无越界值 -> 溢出桶计数为 0；当前一旦越界，none 平滑 PSI=inf
    b = np.arange(0.0, 10.0)
    c = np.concatenate([np.arange(0.0, 9.0), [99.0]])
    result = monitor_feature(b, c, n_bins=5, smoothing="none")
    assert np.isinf(result.psi)
    assert any("空桶" in n for n in result.notes)


def test_all_missing_current_is_flagged_and_caveated() -> None:
    b, c = synthetic.all_missing_current()
    result = monitor_feature(b, c, feature="all_missing")
    assert result.current_all_missing
    assert result.n_current_observed == 0
    assert result.missing_rate_current == 1.0
    assert result.missing_rate_baseline == 0.0
    assert any("不能解读为真实分布漂移" in n for n in result.notes)


def test_small_sample_is_flagged() -> None:
    b, c = synthetic.small_sample(10)
    result = monitor_feature(b, c, feature="tiny", min_sample=30)
    assert result.small_sample
    assert any("阈值打标不构成统计结论" in n for n in result.notes)


def test_empty_current_window_is_all_missing() -> None:
    result = monitor_feature(
        [0.0, 1.0, 2.0, 3.0] * 50, [], feature="empty_window"
    )
    assert result.current_all_missing
    assert result.n_current == 0


def test_missing_rate_delta_detects_missingness_shift() -> None:
    b, c0 = synthetic.same_distribution(seed=3)
    c = synthetic.inject_missing(c0, rate=0.5, seed=9)
    result = monitor_feature(b, c, feature="miss_shift")
    assert result.missing_rate_current == pytest.approx(0.5, abs=0.05)
    assert result.missing_rate_delta > 0.4


def test_overflow_bins_participate_in_psi() -> None:
    b, c0 = synthetic.same_distribution(n_baseline=2000, n_current=1000, seed=5)
    c = synthetic.inject_extremes(c0, rate=0.2, magnitude=50.0, seed=5)
    result = monitor_feature(b, c, feature="extremes", n_bins=10)
    d = result.to_dict()
    # to_dict 的频率数组不含缺值桶，最后一个即上溢出桶
    overflow_mass = d["bins"]["current_freq"][-1]
    assert overflow_mass > 0.1  # 溢出桶有显著质量
    assert result.psi > 0.1


def test_mismatched_bins_rejected() -> None:
    b1 = fit_fixed_bins([0.0, 1.0], n_bins=2)
    b2 = fit_fixed_bins([0.0, 10.0], n_bins=2)
    with pytest.raises(ValueError, match="同一套"):
        compute_drift(assign_counts(b1, [0.5]), assign_counts(b2, [5.0]))


def test_result_dict_is_json_friendly_with_inf() -> None:
    import json

    result = monitor_feature(
        [0.0, 1.0, 2.0] * 40, [2.0] * 100, n_bins=4, smoothing="none"
    )
    text = json.dumps(result.to_dict())  # 不抛异常即合法
    parsed = json.loads(text)
    assert parsed["psi"] in ("inf", "-inf", "nan") or isinstance(parsed["psi"], float)


def test_default_alpha_is_applied() -> None:
    b, c = synthetic.shifted_distribution(seed=42)
    result = monitor_feature(b, c)
    assert result.smoothing == "laplace"
    assert result.alpha == pytest.approx(DEFAULT_ALPHA)
    assert result.epsilon is None
