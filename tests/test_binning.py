"""固定分箱、缺值桶、溢出桶的单元测试。"""
from __future__ import annotations

import numpy as np
import pytest

from drift.binning import (
    MISSING_LABEL,
    OVERFLOW_LABEL,
    UNDERFLOW_LABEL,
    assign_counts,
    bin_labels,
    empirical_frequencies,
    fit_fixed_bins,
    missing_rate,
)


def test_edges_are_equally_spaced_and_inclusive() -> None:
    bins = fit_fixed_bins(np.arange(0.0, 11.0), n_bins=5)
    assert bins.n_bins == 5
    assert bins.total_bins == 8  # 5 内部 + 2 溢出 + 1 缺值
    np.testing.assert_allclose(bins.edges, [0, 2, 4, 6, 8, 10])
    assert bins.width == pytest.approx(2.0)
    assert bins.min_value == 0.0 and bins.max_value == 10.0


def test_assignment_covers_every_special_bucket() -> None:
    # 桶布局: [under][b0..b4][over][missing]，共 8 个
    bins = fit_fixed_bins(np.arange(0.0, 11.0), n_bins=5)
    values = [
        0, 1,        # b0（左边界闭合）
        2,           # b1
        9, 10,       # b4（右边界闭合：10 留在最后内部桶）
        -1, -np.inf, # 下溢出（含负无穷）
        11, np.inf,  # 上溢出（含正无穷）
        np.nan,      # 缺值
    ]
    counts = assign_counts(bins, values)
    assert counts.counts.tolist() == [2, 2, 1, 0, 0, 2, 2, 1]
    assert counts.n_observed == 9
    assert counts.n_missing == 1
    assert counts.n_total == 10


def test_none_is_missing_like_nan() -> None:
    bins = fit_fixed_bins([0.0, 1.0, 2.0], n_bins=2)
    counts = assign_counts(bins, [None, np.nan, 1.0])
    assert counts.n_missing == 2
    assert counts.n_observed == 1


def test_right_endpoint_stays_internal_but_higher_values_overflow() -> None:
    bins = fit_fixed_bins([0.0, 10.0], n_bins=2)  # edges 0,5,10
    counts = assign_counts(bins, [10.0, 10.000001, -0.000001])
    # 恰好等于 max -> 最后内部桶；严格更大 -> 溢出；严格小于 min -> 下溢
    assert counts.counts[bins.n_bins] == 1
    assert counts.counts[bins.n_bins + 1] == 1
    assert counts.counts[0] == 1


def test_inf_does_not_define_edges_but_is_counted_in_overflow() -> None:
    bins = fit_fixed_bins([0.0, 1.0, 2.0, np.inf, -np.inf], n_bins=2)
    assert bins.min_value == 0.0
    assert bins.max_value == 2.0
    counts = assign_counts(bins, [np.inf, -np.inf])
    assert counts.counts[0] == 1  # 下溢
    assert counts.counts[-2] == 1  # 上溢
    assert counts.n_missing == 0


def test_constant_baseline_degenerates_to_single_point_grid() -> None:
    bins = fit_fixed_bins([5.0] * 20, n_bins=10)
    assert bins.n_bins == 1
    np.testing.assert_allclose(bins.edges, [5.0, 5.0])
    counts = assign_counts(bins, [5.0, 5.0, 5.1, 4.9])
    assert counts.counts[1] == 2  # 常值落在唯一内部桶
    assert counts.counts[0] == 1  # 更小 -> 下溢
    assert counts.counts[2] == 1  # 更大 -> 上溢


def test_all_missing_baseline_cannot_be_binned() -> None:
    with pytest.raises(ValueError, match="没有任何有限非缺失值"):
        fit_fixed_bins([np.nan, None], n_bins=5)


def test_invalid_inputs_fail_fast_at_boundary() -> None:
    bins = fit_fixed_bins([0.0, 1.0], n_bins=2)
    with pytest.raises(ValueError, match="非数值类型"):
        assign_counts(bins, ["1.0"])
    with pytest.raises(ValueError, match="布尔值"):
        assign_counts(bins, [True])
    with pytest.raises(ValueError, match="一维"):
        fit_fixed_bins(np.array([[1.0, 2.0], [3.0, 4.0]]))


def test_invalid_n_bins() -> None:
    with pytest.raises(ValueError):
        fit_fixed_bins([1.0, 2.0], n_bins=0)
    with pytest.raises(ValueError):
        fit_fixed_bins([1.0, 2.0], n_bins=True)  # type: ignore[arg-type]


def test_empty_window_all_zero_counts() -> None:
    bins = fit_fixed_bins([0.0, 1.0, 2.0], n_bins=3)
    counts = assign_counts(bins, [])
    assert counts.n_total == 0
    np.testing.assert_array_equal(empirical_frequencies(counts), 0.0)
    assert missing_rate(counts) == 1.0  # 空窗口约定缺值率 1.0


def test_labels_align_with_counts() -> None:
    bins = fit_fixed_bins([0.0, 10.0], n_bins=2)
    labels = bin_labels(bins)
    assert labels[0] == UNDERFLOW_LABEL
    assert labels[-2] == OVERFLOW_LABEL
    assert labels[-1] == MISSING_LABEL
    assert len(labels) == bins.total_bins
    assert labels[1].startswith("[0") and labels[2].endswith("10]")


def test_bins_roundtrip_dict() -> None:
    bins = fit_fixed_bins(np.linspace(-3.0, 3.0, 100), n_bins=7)
    restored = type(bins).from_dict(bins.to_dict())
    np.testing.assert_array_equal(restored.edges, bins.edges)
    assert restored.n_bins == bins.n_bins
