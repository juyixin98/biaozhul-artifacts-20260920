"""Unit tests for binomial kernels against the independent oracle."""
from __future__ import annotations

import math

import pytest
from scipy.stats import binom

from ssp.contracts import Alternative, Allocation
from ssp.kernels import binomial as bk

from reference_oracle import (
    binom_asymp_power_one,
    binom_asymp_power_two,
    binom_exact_critical_one,
    binom_exact_power_one,
    fisher_power_two_bruteforce,
)


class TestExactCriticalRegion:
    @pytest.mark.unit
    @pytest.mark.parametrize("n,p0", [(20, 0.5), (50, 0.1), (100, 0.01), (301, 0.01)])
    def test_greater_region_size_never_exceeds_alpha(self, n, p0):
        c_low, c_high, size = bk._exact_one_sample_critical(n, p0, 0.05, Alternative.GREATER)
        assert c_low == -1
        assert c_high >= 1
        # Direct null-tail mass.
        assert size <= 0.05 + 1e-12
        assert size == pytest.approx(binom.sf(c_high - 1, n, p0), abs=1e-12)

    @pytest.mark.unit
    @pytest.mark.parametrize("n,p0", [(20, 0.5), (60, 0.9), (120, 0.99)])
    def test_less_region_size_never_exceeds_alpha(self, n, p0):
        c_low, c_high, size = bk._exact_one_sample_critical(n, p0, 0.05, Alternative.LESS)
        assert c_high == n + 1
        assert 0 <= c_low < n
        assert size <= 0.05 + 1e-12

    @pytest.mark.unit
    @pytest.mark.parametrize("n,p0", [(30, 0.5), (80, 0.2), (150, 0.02)])
    def test_two_sided_region_respects_half_alpha_per_tail(self, n, p0):
        c_low, c_high, size = bk._exact_one_sample_critical(n, p0, 0.05, Alternative.TWO_SIDED)
        assert binom.cdf(c_low, n, p0) <= 0.025 + 1e-12
        assert binom.sf(c_high - 1, n, p0) <= 0.025 + 1e-12
        assert size <= 0.05 + 1e-12

    @pytest.mark.unit
    def test_conservative_discreteness_at_tiny_n(self):
        # n=5, p0=0.5: the smallest achievable one-sided tail is 1/32=0.03125.
        _, c_high, size = bk._exact_one_sample_critical(5, 0.5, 0.05, Alternative.GREATER)
        assert c_high == 5
        assert size == pytest.approx(1 / 32, abs=1e-12)

    @pytest.mark.unit
    @pytest.mark.parametrize("n,p0,alt", [(50, 0.1, "greater"), (90, 0.5, "two_sided"),
                                          (179, 0.1, "less")])
    def test_critical_region_matches_bruteforce_oracle(self, n, p0, alt):
        alt_enum = Alternative(alt)
        lo_p, hi_p, _ = binom_exact_critical_one(n, p0, 0.05, alt)
        c_low, c_high, _ = bk._exact_one_sample_critical(n, p0, 0.05, alt_enum)
        assert c_low == (-1 if lo_p is None else lo_p)
        assert c_high == (n + 1 if hi_p is None else hi_p)


class TestExactOneSamplePower:
    @pytest.mark.unit
    @pytest.mark.parametrize("n", [50, 150, 300, 301, 500])
    def test_low_base_rate_matches_oracle(self, n):
        kernel = bk.BinomialExactOneSample(0.01, 0.03, 0.05, Alternative.GREATER)
        got = kernel.power(Allocation(n0=n, n1=0))
        expected = binom_exact_power_one(n, 0.01, 0.03, 0.05, "greater")
        assert got == pytest.approx(expected, abs=1e-12)

    @pytest.mark.unit
    def test_n301_passes_n300_fails(self):
        kernel = bk.BinomialExactOneSample(0.01, 0.03, 0.05, Alternative.GREATER)
        assert kernel.power(Allocation(n0=301, n1=0)) >= 0.8
        assert kernel.power(Allocation(n0=300, n1=0)) < 0.8

    @pytest.mark.unit
    def test_extreme_high_base_rate_is_symmetric_to_low(self):
        # Testing p0=.99 vs p1=.97 in the LESS direction mirrors .01 vs .03.
        kernel = bk.BinomialExactOneSample(0.99, 0.97, 0.05, Alternative.LESS)
        got301 = kernel.power(Allocation(n0=301, n1=0))
        got300 = kernel.power(Allocation(n0=300, n1=0))
        expected = binom_exact_power_one(301, 0.99, 0.97, 0.05, "less")
        assert got301 == pytest.approx(expected, abs=1e-12)
        assert got301 >= 0.8 and got300 < 0.8


class TestAsymptoticBinomial:
    @pytest.mark.unit
    @pytest.mark.parametrize("n", [100, 200, 294, 295, 400])
    def test_two_sample_matches_oracle(self, n):
        kernel = bk.BinomialAsymptoticTwoSample(0.2, 0.3, 0.05, Alternative.TWO_SIDED)
        got = kernel.power(Allocation(n0=n, n1=n))
        expected = binom_asymp_power_two(n, n, 0.2, 0.3, 0.05, "two_sided")
        assert got == pytest.approx(expected, abs=1e-12)

    @pytest.mark.unit
    @pytest.mark.parametrize("n", [80, 150, 300])
    def test_one_sample_matches_oracle(self, n):
        kernel = bk.BinomialAsymptoticOneSample(0.3, 0.45, 0.05, Alternative.GREATER)
        got = kernel.power(Allocation(n0=n, n1=0))
        expected = binom_asymp_power_one(n, 0.3, 0.45, 0.05, "greater")
        assert got == pytest.approx(expected, abs=1e-12)

    @pytest.mark.unit
    def test_expected_count_diagnostic_flags_low_base_rate(self):
        alloc = Allocation(n0=100, n1=100)
        assert bk.min_expected_count(0.01, 0.03, alloc, True) < 5
        alloc_ok = Allocation(n0=500, n1=500)
        assert bk.min_expected_count(0.2, 0.3, alloc_ok, True) >= 5


class TestFisherExact:
    @pytest.mark.unit
    @pytest.mark.parametrize("n0,n1", [(40, 40), (80, 80), (123, 122), (126, 126), (160, 160)])
    def test_unconditional_power_matches_full_table_enumeration(self, n0, n1):
        kernel = bk.BinomialExactTwoSample(0.05, 0.15, 0.05, Alternative.GREATER)
        got = kernel.power(Allocation(n0=n0, n1=n1))
        expected = fisher_power_two_bruteforce(n0, n1, 0.05, 0.15, 0.05, "greater")
        assert got == pytest.approx(expected, abs=1e-9)

    @pytest.mark.unit
    def test_fisher_n126_passes_n125_fails(self):
        kernel = bk.BinomialExactTwoSample(0.05, 0.15, 0.05, Alternative.GREATER)
        assert kernel.power(Allocation(n0=126, n1=126)) >= 0.8
        assert kernel.power(Allocation(n0=125, n1=125)) < 0.8

    @pytest.mark.unit
    def test_two_sided_fisher_matches_oracle(self):
        kernel = bk.BinomialExactTwoSample(0.1, 0.3, 0.05, Alternative.TWO_SIDED)
        got = kernel.power(Allocation(n0=70, n1=70))
        expected = fisher_power_two_bruteforce(70, 70, 0.1, 0.3, 0.05, "two_sided")
        assert got == pytest.approx(expected, abs=1e-9)

    @pytest.mark.unit
    def test_fisher_size_under_common_p_is_bounded_by_alpha(self):
        # Under p0=p1 the test is conditional and exact: rejection probability
        # at any fixed margin <= alpha; averaged over margins it stays <= alpha.
        kernel = bk.BinomialExactTwoSample(0.1, 0.1, 0.05, Alternative.GREATER)
        size = kernel.power(Allocation(n0=80, n1=80))
        assert size <= 0.05 + 1e-9
