"""Tests for chunked merge algorithms.

The central claim under test is that compensation *state* must cross block
boundaries.  Two ways of "cheating" are explicitly distinguished:

* ``blocked_naive`` threads only a running total and must equal naive exactly;
* ``naive_sharded`` adds per-block totals with ordinary float adds and must
  lose accuracy on ill-conditioned input, while ``kahan_merged`` keeps it.
"""
from __future__ import annotations

import math

import numpy as np

from app.core import chunking, kernels
from app.services import synthetic


def test_blocked_naive_is_bit_identical_to_naive_for_all_block_sizes():
    values = synthetic.mixed_scales(5000, seed=4)
    expected = kernels.naive_sum(values)
    for bs in (1, 2, 3, 7, 4096):
        assert chunking.blocked_naive(values, bs) == expected


def test_streaming_kahan_is_bit_identical_to_monolithic_kahan():
    values = synthetic.big_cancel(100_000)
    monolithic = kernels.kahan_sum(values)
    assert monolithic == 100_000.0
    for bs in (1, 5, 256, 4096, 1_000_000):
        assert chunking.blocked_kahan_streaming(values, bs) == monolithic


def test_naive_sharded_anti_pattern_loses_small_terms():
    # 100_000 ones between ±1e16, blocks of 4096: each block loses the
    # fraction of ones below ulp(1e16) after the big positive lands.
    values = synthetic.big_cancel(100_000)
    cheated = chunking.naive_sharded_totals(values, 4096)
    assert cheated == 95_904.0
    assert abs(cheated - 100_000.0) == 4096.0


def test_double_double_merged_kahan_recovers_where_anti_pattern_fails():
    values = synthetic.big_cancel(100_000)
    # With block boundaries that align the big ±1e16 markers inside shards
    # the double-double merge is exact; with power-of-two boundaries the
    # first shard honestly retains a +1 residual, so the result is 100001
    # (absolute error 1) - compensated-order accuracy either way, and in
    # both cases dramatically better than the local-totals anti-pattern.
    assert chunking.blocked_kahan_merged(values, 7) == 100_000.0
    merged_4096 = chunking.blocked_kahan_merged(values, 4096)
    assert merged_4096 == 100_001.0
    assert abs(merged_4096 - 100_000.0) == 1.0
    assert abs(merged_4096 - 100_000.0) < abs(
        chunking.naive_sharded_totals(values, 4096) - 100_000.0
    )


def test_merged_kahan_with_block_size_one_is_single_kahan_pass():
    values = synthetic.mixed_scales(2000, seed=9)
    assert chunking.blocked_kahan_merged(values, 1) == kernels.kahan_sum(values)


def test_pairwise_block_merge_is_balanced():
    # Block totals [M, 1, 1, 1, 1, 1, 1, 1, -M] (9 blocks, bs=2 with zero
    # padding).  A left fold absorbs every 1 into M's ulp and returns 0;
    # the balanced tree combines the small blocks first (1+1, 1+1+1+1, ...)
    # so their total is large enough to survive when M finally joins,
    # recovering 6 of the 7 ones.
    blocks = [[1e16, 0.0]] + [[1.0, 0.0]] * 7 + [[-1e16, 0.0]]
    values = np.array([x for pair in blocks for x in pair], dtype=np.float64)
    result = chunking.blocked_pairwise(values, 2)
    assert result == 6.0
    assert chunking.naive_sharded_totals(values, 2) == 0.0


def test_blocked_pairwise_on_power_of_two_width_may_equal_left_fold():
    # Documented non-mystery: when every block total is already exactly
    # representable and aligned to the big accumulator's ulp, tree merging
    # cannot conjure lost bits either - it is not Kahan.  Concrete locked
    # value for the wide-cancellation input at bs=4096.
    values = synthetic.big_cancel(100_000)
    assert chunking.blocked_pairwise(values, 4096) == 95_904.0
    assert chunking.naive_sharded_totals(values, 4096) == 95_904.0


def test_two_sum_is_error_free():
    # Exactness a + b == hi + lo is verified with arbitrary-precision
    # rational arithmetic (mpmath), not with float64 arithmetic that would
    # itself round the reconstruction.
    import mpmath

    mpmath.mp.dps = 80
    rng = np.random.default_rng(0)
    for a, b in rng.uniform(-1e12, 1e12, size=(500, 2)):
        fa, fb = float(a), float(b)
        hi, lo = chunking.two_sum(fa, fb)
        assert mpmath.mpf(fa) + mpmath.mpf(fb) == mpmath.mpf(hi) + mpmath.mpf(lo)
        # Non-overlap of the two words: |lo| <= 0.5 ulp(hi).
        assert abs(lo) <= 0.5 * math.ulp(abs(hi))

    # Classic case: 1 + 2^-53 rounds to 1.0, the residual survives in lo.
    hi, lo = chunking.two_sum(1.0, 2.0 ** -53)
    assert hi == 1.0
    assert lo == 2.0 ** -53


def test_chunked_special_values_follow_same_policy():
    values = np.array([math.inf, -math.inf, 1.0])
    assert math.isnan(chunking.blocked_kahan_streaming(values, 2))
    assert math.isnan(chunking.blocked_kahan_merged(values, 2))
    assert math.isnan(chunking.blocked_pairwise(values, 2))
    assert math.isnan(chunking.naive_sharded_totals(values, 2))

    zeros = np.array([-0.0, -0.0])
    assert math.copysign(1.0, chunking.blocked_kahan_merged(zeros, 1)) == -1.0
