"""Tests for fixed binning: boundaries, special buckets, ties, persistence."""
import numpy as np
import pytest

from drift_monitor.binning import FixedBinner, bucket_labels


def test_quantile_edges_partition_baseline_evenly():
    x = np.arange(1000, dtype=np.float64)
    b = FixedBinner(n_bins=10).fit(x)
    counts = b.counts(x)
    # Underflow/overflow empty for values spanning the edges exactly;
    # finite buckets hold ~100 each (quantile ties can shift a few).
    assert counts[0] == 0 and counts[-2] == 0
    assert counts[-1] == 0  # no missing
    finite = counts[1:-2]
    assert finite.sum() == 1000
    assert np.max(finite) - np.min(finite) <= 2


def test_right_edge_inclusive_and_overflow_underflow():
    b = FixedBinner(n_bins=3, strategy="custom",
                    edges=np.array([0.0, 1.0, 2.0, 3.0]))
    idx = b.transform(np.array([-1.0, 0.0, 0.5, 2.0, 3.0, 3.5, np.nan]))
    # underflow, finite1, finite1, finite3 ([2,3)),
    # finite3 (3.0 == right edge, inclusive), overflow, missing
    assert idx.tolist() == [0, 1, 1, 3, 3, 4, 5]
    counts = b.counts(np.array([-1.0, 0.0, 0.5, 2.0, 3.0, 3.5, np.nan]))
    assert counts.tolist() == [1, 2, 0, 2, 1, 1]
    assert counts.size == b.n_buckets == 6


def test_half_open_interior_buckets():
    b = FixedBinner(n_bins=2, strategy="custom",
                    edges=np.array([0.0, 1.0, 2.0]))
    idx = b.transform(np.array([1.0]))
    assert idx.tolist() == [2]  # [1, 2) not [0, 1)


def test_missing_has_dedicated_bucket():
    x = np.array([1.0, np.nan, 3.0, np.nan])
    b = FixedBinner(n_bins=2, strategy="custom",
                    edges=np.array([0.0, 2.0, 4.0]))
    assert b.counts(x)[-1] == 2


def test_baseline_with_missing_fits_on_finite_only():
    x = np.array([1.0, 2.0, np.nan, 3.0, 4.0])
    b = FixedBinner(n_bins=3).fit(x)
    counts = b.counts(x)
    assert counts[-1] == 1
    assert counts.sum() == 5


def test_constant_feature_does_not_crash():
    b = FixedBinner(n_bins=5).fit(np.full(100, 7.0))
    counts = b.counts(np.full(100, 7.0))
    assert counts.sum() == 100
    assert counts[0] == 0 and counts[-2] == 0  # constant sits inside finite bins
    shifted = b.counts(np.full(10, 700.0))
    assert shifted[-2] == 10  # far-away value overflows


def test_quantile_ties_keep_fixed_bucket_count():
    rng = np.random.default_rng(0)
    x = rng.choice([0.0, 1.0, 2.0], size=500, p=[0.8, 0.1, 0.1])
    b = FixedBinner(n_bins=10).fit(x)
    cur = rng.choice([0.0, 1.0, 2.0, 3.0], size=200)
    assert b.counts(x).size == b.counts(cur).size == 13


def test_uniform_strategy_widths_equal():
    x = np.linspace(0.0, 10.0, 101)
    b = FixedBinner(n_bins=5, strategy="uniform").fit(x)
    widths = np.diff(b.edges)
    assert np.allclose(widths, widths[0])
    assert np.isclose(widths[0], 2.0)


def test_all_missing_baseline_raises():
    with pytest.raises(ValueError, match="all-missing"):
        FixedBinner(n_bins=3).fit(np.full(10, np.nan))


def test_custom_edges_validation():
    with pytest.raises(ValueError, match="strictly|non-decreasing"):
        FixedBinner(n_bins=2, strategy="custom",
                    edges=np.array([2.0, 1.0, 0.0]))
    with pytest.raises(ValueError, match="NaN"):
        FixedBinner(n_bins=2, strategy="custom",
                    edges=np.array([0.0, np.nan, 1.0]))
    with pytest.raises(ValueError, match="full edges"):
        FixedBinner(n_bins=2, strategy="custom", edges=np.array([0.0, 1.0]))


def test_open_ended_custom_edges_with_infinities():
    b = FixedBinner(n_bins=3, strategy="custom",
                    edges=np.array([-np.inf, 0.0, 1.0, np.inf]))
    idx = b.transform(np.array([-50.0, 0.5, 50.0]))
    assert idx.tolist() == [1, 2, 3]
    labels = bucket_labels(b)
    assert labels[1].startswith("[-inf")
    assert labels[3].endswith("+inf]")


def test_round_trip_serialization():
    b = FixedBinner(n_bins=4).fit(np.random.default_rng(1).normal(size=200))
    payload = b.to_dict()
    restored = FixedBinner.from_dict(payload)
    x = np.linspace(-3.0, 3.0, 50)
    assert np.array_equal(restored.counts(x), b.counts(x))
