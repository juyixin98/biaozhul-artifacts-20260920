"""Tests for the three monolithic kernels.

All assertions are on *concrete* results (exact float literals) or on
mathematical identities, never on "the call succeeded".
"""
from __future__ import annotations

import math

import numpy as np

from app.core import kernels
from app.services import synthetic

U = 2.0 ** -53


def test_naive_repeating_one_tenth_has_known_rounding_drift():
    values = synthetic.repeating_decimal(10, 0.1)
    assert kernels.naive_sum(values) == 0.9999999999999999
    # Kahan compensates the per-add rounding.
    assert kernels.kahan_sum(values) == 1.0


def test_naive_loses_small_terms_against_large_magnitude():
    # [1e16, 1, ..., 1, -1e16]: every 1.0 is below the running sum's ulp.
    values = synthetic.big_cancel(10)
    assert kernels.naive_sum(values) == 0.0
    assert kernels.kahan_sum(values) == 10.0


def test_kahan_recovers_one_hundred_thousand_lost_ones():
    values = synthetic.big_cancel(100_000)
    assert kernels.naive_sum(values) == 0.0
    assert kernels.kahan_sum(values) == 100_000.0
    # Pairwise is not magic: with these widths it loses a known, bounded
    # fraction of the ones (locked as a concrete value).
    assert kernels.pairwise_sum(values) == 99_974.0


def test_tiny_tail_accumulates_only_under_compensation():
    values = synthetic.eps_tail(100)
    assert kernels.naive_sum(values) == 1.0
    # 100 * 2^-53 rounds to 50 binary64 ulps.
    assert kernels.kahan_sum(values) == 1.0 + 100 * U


def test_pairwise_matches_sequential_for_small_inputs():
    values = np.array([0.1] * 10)
    assert kernels.pairwise_sum(values) == kernels.naive_sum(values)


def test_empty_input_returns_positive_zero():
    empty = np.array([], dtype=np.float64)
    info = kernels.assess(empty)
    assert kernels.naive_sum(empty, info) == 0.0
    assert math.copysign(1.0, kernels.naive_sum(empty, info)) == 1.0
    assert kernels.pairwise_sum(empty, info) == 0.0
    assert kernels.kahan_sum(empty, info) == 0.0


def test_signed_zero_rules_are_fixed():
    only_negative = kernels.naive_sum(np.array([-0.0, -0.0]))
    assert only_negative == 0.0
    assert math.copysign(1.0, only_negative) == -1.0

    mixed = kernels.naive_sum(np.array([-0.0, 0.0]))
    assert math.copysign(1.0, mixed) == 1.0

    exact_cancellation = kernels.naive_sum(np.array([-0.0, 1.0, -1.0]))
    assert math.copysign(1.0, exact_cancellation) == 1.0


def test_nan_input_returns_quiet_nan_regardless_of_position():
    values = np.array([1.0, math.nan, math.inf, -math.inf])
    for kernel in (kernels.naive_sum, kernels.kahan_sum, kernels.pairwise_sum):
        result = kernel(values)
        assert math.isnan(result)


def test_infinites_policy():
    assert kernels.naive_sum(np.array([1.0, math.inf, 2.0])) == math.inf
    assert kernels.naive_sum(np.array([1.0, -math.inf, 2.0])) == -math.inf
    # +inf and -inf together is an invalid operation.
    assert math.isnan(kernels.naive_sum(np.array([math.inf, -math.inf])))
    # NaN beats the infinity conflict.
    assert math.isnan(kernels.naive_sum(np.array([math.nan, math.inf, -math.inf])))


def test_assess_census_is_order_independent():
    a = np.array([1.0, -math.inf, math.inf, -0.0, 0.0, math.nan])
    info = kernels.assess(a)
    assert info.has_nan is True
    assert info.pos_inf == 1
    assert info.neg_inf == 1
    assert info.pos_zero == 1
    assert info.neg_zero == 1
    assert info.nonzero_finite == 1
    assert info.n_elements == 6


def test_integer_prefix_sums_are_exact_for_all_kernels():
    values = np.arange(1.0, 1001.0)  # exact up to 2^53
    expected = 1000 * 1001 / 2
    assert kernels.naive_sum(values) == expected
    assert kernels.kahan_sum(values) == expected
    assert kernels.pairwise_sum(values) == expected
