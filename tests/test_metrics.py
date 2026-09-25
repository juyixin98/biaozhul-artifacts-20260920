"""Tests for drift metrics: identity, symmetry, smoothing, edge cases."""
import numpy as np
import pytest

from drift_monitor.metrics import (
    psi, js_divergence, total_variation, wasserstein_1, smooth_probs,
    psi_band, PSI_STABLE, PSI_WARNING,
)


def test_identical_distributions_have_zero_metrics():
    counts = np.array([10, 40, 30, 20])
    assert psi(counts, counts) == pytest.approx(0.0, abs=1e-12)
    assert js_divergence(counts, counts) == pytest.approx(0.0, abs=1e-12)
    assert total_variation(counts, counts) == pytest.approx(0.0, abs=1e-12)


def test_psi_matches_reference_formula_on_simple_case():
    # p = [0.9, 0.1], q = [0.1, 0.9], smoothing off
    p = np.array([90, 10])
    q = np.array([10, 90])
    expected = (0.1 - 0.9) * np.log(0.1 / 0.9) + (0.9 - 0.1) * np.log(0.9 / 0.1)
    assert psi(p, q, alpha=0.0) == pytest.approx(expected)
    assert psi(p, q, alpha=0.0) > PSI_WARNING


def test_psi_is_symmetric_in_the_two_windows():
    a = np.array([500, 480, 20])
    b = np.array([490, 505, 5])
    assert psi(a, b) == pytest.approx(psi(b, a), rel=1e-12)


def test_empty_bucket_without_smoothing_gives_infinite_psi():
    p = np.array([50, 50])
    q = np.array([100, 0])
    assert np.isinf(psi(p, q, alpha=0.0))


def test_smoothing_keeps_psi_finite_and_lowers_it():
    p = np.array([50, 50])
    q = np.array([100, 0])
    smoothed = psi(p, q, alpha=0.5)
    assert np.isfinite(smoothed)
    assert smoothed > 0.5  # a complete evacuation is still a big shift


def test_smooth_probs_are_normalized_and_weak():
    p, q = smooth_probs(np.array([10, 0]), np.array([0, 10]), alpha=0.5)
    assert p.sum() == pytest.approx(1.0)
    assert q.sum() == pytest.approx(1.0)
    # no zero entries remain
    assert np.all(p > 0) and np.all(q > 0)


def test_js_symmetric_and_bounded_by_ln2():
    p = np.array([80, 10, 10])
    q = np.array([5, 60, 35])
    assert js_divergence(p, q) == pytest.approx(js_divergence(q, p))
    assert 0.0 <= js_divergence(p, q) < np.log(2)


def test_js_finite_with_disjoint_support_no_smoothing():
    p = np.array([100, 0])
    q = np.array([0, 100])
    assert np.isfinite(js_divergence(p, q, alpha=0.0))
    assert total_variation(p, q, alpha=0.0) == pytest.approx(1.0)


def test_tv_known_value():
    p = np.array([70, 30])
    q = np.array([40, 60])
    assert total_variation(p, q, alpha=0.0) == pytest.approx(0.3)


def test_wasserstein_zero_for_identical_and_positive_for_shift():
    edges = np.array([0.0, 1.0, 2.0, 3.0])  # n_bins=3 -> 6 buckets
    c = np.array([0, 25, 25, 25, 0, 0])  # uniform across the 3 finite bins
    assert wasserstein_1(c, c, edges) == pytest.approx(0.0, abs=1e-12)
    shifted = np.array([0, 0, 25, 25, 50, 0])  # mass moved toward overflow
    assert wasserstein_1(c, shifted, edges) > 0.3


def test_wasserstein_units_match_feature_and_underflow_counts():
    edges = np.array([0.0, 1.0, 2.0])  # n_bins=2 -> 5 buckets
    # all baseline mass in finite bucket [0,1) center .5;
    # all current mass underflow parked at 0 -> distance 0.5
    base = np.array([0, 100, 0, 0, 0])
    cur = np.array([100, 0, 0, 0, 0])
    assert wasserstein_1(base, cur, edges) == pytest.approx(0.5)


def test_wasserstein_all_missing_one_side_is_nan():
    edges = np.array([0.0, 1.0])
    base = np.array([0, 10, 0, 0])
    cur = np.array([0, 0, 0, 0, 10])  # length mismatch -> guard the test below
    with pytest.raises(ValueError):
        wasserstein_1(base, cur, edges)
    # Correct-length vector: mass entirely in the missing bucket -> NaN
    cur = np.array([0, 0, 0, 10])
    assert np.isnan(wasserstein_1(base, cur, edges))


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        psi(np.array([1, -1]), np.array([1, 1]))
    with pytest.raises(ValueError):
        smooth_probs(np.array([1, 2]), np.array([1, 2, 3]))
    with pytest.raises(ValueError, match="alpha"):
        smooth_probs(np.array([1]), np.array([1]), alpha=-0.1)


def test_psi_band_labels():
    assert psi_band(0.0) == "stable"
    assert psi_band(PSI_STABLE) == "moderate"
    assert psi_band(0.2) == "moderate"
    assert psi_band(PSI_WARNING) == "significant"
