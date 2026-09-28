"""Unit tests for the normal kernels, validated against the independent oracle.

The oracle (tests/reference_oracle.py) recomputes power directly from
scipy.stats primitives and never imports the production kernels.
"""
from __future__ import annotations

import math

import pytest
from scipy.stats import norm

from ssp.contracts import Alternative, Allocation
from ssp.errors import NoncentralityError
from ssp.kernels import normal as nk

from reference_oracle import (
    normal_t_power_one,
    normal_z_power_one,
    normal_z_power_two,
)


class TestContinuousEstimate:
    @pytest.mark.unit
    def test_one_sample_matches_textbook_constant(self):
        # (z_.975 + z_.8)^2 = (1.959964 + 0.841621)^2 = 7.848886; /d^2 with d=.5 -> 31.3955
        n = nk.continuous_n0(
            d_abs=0.5, alpha=0.05, target_power=0.8,
            alternative=Alternative.TWO_SIDED, two_sample=False, allocation_ratio=1.0,
        )
        assert n == pytest.approx((norm.ppf(0.975) + norm.ppf(0.8)) ** 2 / 0.25)

    @pytest.mark.unit
    def test_two_sample_equal_allocation_is_four_times_one_sample_per_arm(self):
        one = nk.continuous_n0(0.3, 0.05, 0.8, Alternative.TWO_SIDED, False, 1.0)
        two_n0 = nk.continuous_n0(0.3, 0.05, 0.8, Alternative.TWO_SIDED, True, 1.0)
        assert two_n0 == pytest.approx(2.0 * one)

    @pytest.mark.unit
    def test_allocation_ratio_two_halves_control_size(self):
        r1 = nk.continuous_n0(0.3, 0.05, 0.9, Alternative.GREATER, True, 1.0)
        r2 = nk.continuous_n0(0.3, 0.05, 0.9, Alternative.GREATER, True, 2.0)
        # n0(r) = base*(1+1/r); ratio n0(2)/n0(1) = (1.5)/(2) = 0.75
        assert r2 / r1 == pytest.approx(0.75)


class TestNoncentralMachinery:
    @pytest.mark.unit
    def test_selfcheck_central_t_matches_quantiles(self):
        report = nk.validate_noncentral_t()
        worst = max(abs(c["cdf"] - c["p"]) for c in report["size_checks"])
        assert worst < 1e-9

    @pytest.mark.unit
    def test_selfcheck_large_df_converges_to_normal(self):
        report = nk.validate_noncentral_t()
        final = report["large_df_convergence"][-1]
        assert final["n"] == 500
        assert final["abs_gap"] < 5e-4

    @pytest.mark.unit
    def test_nct_cdf_rejects_nonfinite_input_with_category(self):
        with pytest.raises(NoncentralityError) as exc:
            nk._nct_cdf(0.0, 0.0, 1.0)  # df=0 is invalid for t
        assert exc.value.category.value == "noncentrality_error"


class TestZKernelVsOracle:
    @pytest.mark.unit
    @pytest.mark.parametrize("n", [10, 20, 31, 32, 50, 100])
    def test_one_sample_power_matches_independent_norm_integral(self, n):
        model = nk.NormalModel(0.5, 0.05, Alternative.TWO_SIDED, False, True)
        got = model.power(Allocation(n0=n, n1=0))
        expected = normal_z_power_one(n, 0.5, 0.05, "two_sided")
        assert got == pytest.approx(expected, abs=1e-12)

    @pytest.mark.unit
    @pytest.mark.parametrize("direction,alt", [("greater", Alternative.GREATER),
                                                ("less", Alternative.LESS)])
    def test_one_sided_directions(self, direction, alt):
        model = nk.NormalModel(0.5, 0.05, alt, False, True)
        got = model.power(Allocation(n0=40, n1=0))
        expected = normal_z_power_one(40, 0.5, 0.05, direction)
        assert got == pytest.approx(expected, abs=1e-12)

    @pytest.mark.unit
    @pytest.mark.parametrize("n0,n1", [(10, 20), (392, 393), (100, 300)])
    def test_two_sample_power_matches_oracle(self, n0, n1):
        model = nk.NormalModel(0.2, 0.05, Alternative.TWO_SIDED, True, True)
        got = model.power(Allocation(n0=n0, n1=n1))
        expected = normal_z_power_two(n0, n1, 0.2, 0.05, "two_sided")
        assert got == pytest.approx(expected, abs=1e-12)

    @pytest.mark.unit
    def test_zero_df_returns_zero_power_instead_of_crashing(self):
        model = nk.NormalModel(0.5, 0.05, Alternative.TWO_SIDED, True, False)
        assert model.power(Allocation(n0=1, n1=1)) == 0.0


class TestTKernelVsOracle:
    @pytest.mark.unit
    @pytest.mark.parametrize("n", [5, 10, 33, 34, 60])
    def test_one_sample_t_power_matches_independent_nct(self, n):
        model = nk.NormalModel(0.5, 0.05, Alternative.TWO_SIDED, False, False)
        got = model.power(Allocation(n0=n, n1=0))
        expected = normal_t_power_one(n, 0.5, 0.05, "two_sided")
        assert got == pytest.approx(expected, abs=1e-10)

    @pytest.mark.unit
    def test_unknown_sigma_requires_more_subjects_than_z(self):
        t_model = nk.NormalModel(0.5, 0.05, Alternative.TWO_SIDED, False, False)
        z_model = nk.NormalModel(0.5, 0.05, Alternative.TWO_SIDED, False, True)
        n = 20
        assert t_model.power(Allocation(n0=n, n1=0)) < z_model.power(Allocation(n0=n, n1=0))


class TestSizeCalibration:
    @pytest.mark.unit
    @pytest.mark.parametrize("n", [20, 60, 200])
    def test_z_test_size_under_null_is_alpha(self, n):
        # Power under d=0 (the null) must equal alpha.
        model = nk.NormalModel(1e-12, 0.05, Alternative.TWO_SIDED, False, True)
        size = model.power(Allocation(n0=n, n1=0))
        assert size == pytest.approx(0.05, abs=1e-6)

    @pytest.mark.unit
    @pytest.mark.parametrize("n", [5, 20, 60])
    def test_t_test_size_under_null_is_alpha(self, n):
        # Non-centrality zero: nct with nc=0 is the central t.
        from scipy.stats import t as t_dist
        df = n - 1
        crit = t_dist.ppf(0.975, df)
        size = 1.0 - nk._nct_cdf(crit, df, 0.0) + nk._nct_cdf(-crit, df, 0.0)
        assert size == pytest.approx(0.05, abs=1e-12)
