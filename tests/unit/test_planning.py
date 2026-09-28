"""Unit tests for planning orchestration: integer search and failure classes."""
from __future__ import annotations

import pytest

from ssp.contracts import (
    Alternative,
    BinomialEffectScale,
    BinomialSpec,
    MethodPreference,
    NormalEffectScale,
    NormalSpec,
)
from ssp.errors import (
    ApproximationInvalidError,
    EffectTooSmallError,
    ExactCapExceededError,
    ValidationError,
)
from ssp.planning import (
    derive_allocation,
    integer_search,
    n0_cap_for_total,
    plan_binomial,
    plan_normal,
)

from reference_oracle import (
    binom_asymp_power_two,
    binom_exact_power_one,
    normal_z_power_one,
)


class TestAllocationDerivation:
    @pytest.mark.unit
    def test_equal_ratio_rounds_n1_to_n0(self):
        a = derive_allocation(5, 1.0, True)
        assert (a.n0, a.n1) == (5, 5)

    @pytest.mark.unit
    def test_ratio_two(self):
        assert (derive_allocation(3, 2.0, True).n0, derive_allocation(3, 2.0, True).n1) == (3, 6)

    @pytest.mark.unit
    def test_n1_is_monotone_non_decreasing_in_n0(self):
        n1s = [derive_allocation(n0, 1.0, True).n1 for n0 in range(1, 50)]
        assert all(b >= a for a, b in zip(n1s, n1s[1:]))

    @pytest.mark.unit
    def test_n0_cap_respects_total_cap(self):
        cap_n0 = n0_cap_for_total(300, 1.0, True)
        assert derive_allocation(cap_n0, 1.0, True).total <= 300
        assert derive_allocation(cap_n0 + 1, 1.0, True).total > 300


class TestIntegerSearchBoundary:
    @pytest.mark.unit
    def test_finds_n32_and_reports_n31_failure_for_textbook_case(self, logger):
        def power(alloc):
            return normal_z_power_one(alloc.n0, 0.5, 0.05, "two_sided")

        outcome = integer_search(
            power, start_n0=31, allocation_ratio=1.0, two_sample=False,
            n0_cap=10_000, target_power=0.8, logger=logger,
        )
        assert outcome.allocation.n0 == 32
        assert outcome.achieved_power >= 0.8
        assert outcome.power_minus_one < 0.8
        assert outcome.allocation_minus_one.n0 == 31

    @pytest.mark.unit
    def test_starting_below_boundary_expands_then_bisects(self, logger):
        calls = []

        def power(alloc):
            calls.append(alloc.n0)
            return normal_z_power_one(alloc.n0, 0.5, 0.05, "two_sided")

        outcome = integer_search(
            power, start_n0=2, allocation_ratio=1.0, two_sample=False,
            n0_cap=10_000, target_power=0.8, logger=logger,
        )
        assert outcome.allocation.n0 == 32
        # Far fewer evaluations than a naive linear scan (needs 32).
        assert len(calls) < 20

    @pytest.mark.unit
    def test_cap_exceeded_raises_typed_error(self, logger):
        def power(alloc):
            return binom_exact_power_one(alloc.n0, 0.01, 0.03, 0.05, "greater")

        with pytest.raises(ExactCapExceededError) as exc:
            integer_search(
                power, start_n0=10, allocation_ratio=1.0, two_sample=False,
                n0_cap=50, target_power=0.8, logger=logger,
            )
        assert exc.value.category.value == "exact_cap_exceeded"
        assert exc.value.details["power_at_cap"] < 0.8


class TestNormalPlans:
    @pytest.mark.unit
    def test_z_plan_is_minimal_and_committed(self, logger):
        spec = NormalSpec(
            Alternative.TWO_SIDED, 0.05, 0.8, 0.5,
            NormalEffectScale.STANDARDIZED_D, False, known_sigma=True,
        ).validate()
        result = plan_normal(spec, "run-z", "fp", logger)
        assert result.allocation.n0 == 32
        assert result.achieved_power == pytest.approx(0.80743, abs=1e-4)
        assert result.power_at_total_minus_one == pytest.approx(0.79501, abs=2e-3)
        assert result.is_committed()
        assert result.method == "normal_z_known_sigma"
        assert result.noncentrality == pytest.approx(0.5 * 32 ** 0.5)

    @pytest.mark.unit
    def test_t_plan_needs_more_than_z_plan(self, logger):
        spec = NormalSpec(
            Alternative.TWO_SIDED, 0.05, 0.8, 0.5,
            NormalEffectScale.STANDARDIZED_D, False, known_sigma=False,
        ).validate()
        result = plan_normal(spec, "run-t", "fp", logger)
        assert result.allocation.n0 == 34
        assert result.is_committed()

    @pytest.mark.unit
    def test_effect_tending_to_zero_is_classified_not_returned_as_plan(self, logger):
        spec = NormalSpec(
            Alternative.TWO_SIDED, 0.05, 0.999, 1e-9,
            NormalEffectScale.STANDARDIZED_D, False, known_sigma=True,
        ).validate()
        with pytest.raises(EffectTooSmallError) as exc:
            plan_normal(spec, "run-small", "fp", logger)
        assert exc.value.category.value == "effect_too_small"

    @pytest.mark.unit
    def test_exact_zero_effect_is_validation_failure(self):
        with pytest.raises(ValidationError):
            NormalSpec(
                Alternative.TWO_SIDED, 0.05, 0.8, 0.0,
                NormalEffectScale.STANDARDIZED_D, False,
            ).validate()

    @pytest.mark.unit
    def test_one_sided_greater_plan(self, logger):
        spec = NormalSpec(
            Alternative.GREATER, 0.05, 0.8, 0.5,
            NormalEffectScale.STANDARDIZED_D, False, known_sigma=True,
        ).validate()
        result = plan_normal(spec, "run-g", "fp", logger)
        # One-sided classic: n=25 (z .95=1.645, z .8=.842): 6.18/.25=24.7 -> 25
        assert result.allocation.n0 == 25
        assert result.is_committed()


class TestBinomialPlans:
    @pytest.mark.unit
    def test_low_base_rate_auto_switches_to_exact(self, logger):
        spec = BinomialSpec(
            Alternative.GREATER, 0.05, 0.8, 0.01, 0.03,
            BinomialEffectScale.PROPORTIONS, False,
        ).validate()
        result = plan_binomial(spec, "run-low", "fp", logger)
        assert result.method == "binomial_exact_one_sample"
        assert result.allocation.n0 == 301
        assert result.is_committed()
        assert any("switched to exact" in w for w in result.warnings)

    @pytest.mark.unit
    def test_moderate_proportions_use_asymptotic(self, logger):
        spec = BinomialSpec(
            Alternative.TWO_SIDED, 0.05, 0.8, 0.2, 0.3,
            BinomialEffectScale.PROPORTIONS, True,
        ).validate()
        result = plan_binomial(spec, "run-mod", "fp", logger)
        assert result.method == "binomial_normal_approx_two_sample"
        assert result.allocation.n0 == 295
        assert result.is_committed()

    @pytest.mark.unit
    def test_forced_asymptotic_at_low_rate_fails_with_explicit_category(self, logger):
        spec = BinomialSpec(
            Alternative.GREATER, 0.05, 0.8, 0.01, 0.03,
            BinomialEffectScale.PROPORTIONS, False,
            method_preference=MethodPreference.ASYMPTOTIC,
        ).validate()
        with pytest.raises(ApproximationInvalidError) as exc:
            plan_binomial(spec, "run-force", "fp", logger)
        assert exc.value.category.value == "approximation_invalid"

    @pytest.mark.unit
    def test_extreme_high_base_rate_exact(self, logger):
        spec = BinomialSpec(
            Alternative.LESS, 0.05, 0.8, 0.99, 0.97,
            BinomialEffectScale.PROPORTIONS, False,
        ).validate()
        result = plan_binomial(spec, "run-high", "fp", logger)
        assert result.method == "binomial_exact_one_sample"
        assert result.allocation.n0 == 301
        assert result.is_committed()

    @pytest.mark.unit
    def test_fisher_two_sample_minimal_boundary(self, logger):
        spec = BinomialSpec(
            Alternative.GREATER, 0.05, 0.8, 0.05, 0.15,
            BinomialEffectScale.PROPORTIONS, True,
            method_preference=MethodPreference.EXACT,
        ).validate()
        result = plan_binomial(spec, "run-fisher", "fp", logger)
        assert (result.allocation.n0, result.allocation.n1) == (126, 126)
        assert result.achieved_power == pytest.approx(0.80141, abs=2e-3)
        assert result.power_at_total_minus_one < 0.8
        assert result.is_committed()

    @pytest.mark.unit
    def test_fisher_unequal_ratio_two_matches_oracle_boundary(self, logger):
        # r=2 (n1 = 2*n0), low-rate endpoint. Oracle scan gives n0=51,n1=102.
        spec = BinomialSpec(
            Alternative.GREATER, 0.05, 0.8, 0.05, 0.20,
            BinomialEffectScale.PROPORTIONS, True, allocation_ratio=2.0,
            method_preference=MethodPreference.EXACT,
        ).validate()
        result = plan_binomial(spec, "run-fisher-r2", "fp", logger)
        assert (result.allocation.n0, result.allocation.n1) == (51, 102)
        assert result.achieved_power == pytest.approx(0.80780, abs=2e-3)
        assert result.power_at_total_minus_one == pytest.approx(0.79902, abs=2e-3)
        assert result.is_committed()

    @pytest.mark.unit
    def test_z_unequal_ratio_two_matches_oracle_boundary(self, logger):
        spec = NormalSpec(
            Alternative.GREATER, 0.05, 0.9, 0.3,
            NormalEffectScale.STANDARDIZED_D, True, known_sigma=True,
            allocation_ratio=2.0,
        ).validate()
        result = plan_normal(spec, "run-z-r2", "fp", logger)
        assert (result.allocation.n0, result.allocation.n1) == (143, 286)
        assert result.achieved_power == pytest.approx(0.90048, abs=2e-3)
        assert result.power_at_total_minus_one < 0.9
        assert result.is_committed()

    @pytest.mark.unit
    def test_tiny_effect_binomial_hits_cap_with_typed_failure(self, logger, settings):
        # p0=.5 vs p1=.5001 needs ~4 million subjects; exact cap is 200k.
        spec = BinomialSpec(
            Alternative.GREATER, 0.05, 0.8, 0.5, 0.5001,
            BinomialEffectScale.PROPORTIONS, False,
            method_preference=MethodPreference.EXACT,
        ).validate()
        with pytest.raises(ExactCapExceededError):
            plan_binomial(spec, "run-cap", "fp", logger, settings)

    @pytest.mark.unit
    def test_effect_scales_risk_difference_and_relative_risk(self, logger):
        spec_rd = BinomialSpec(
            Alternative.GREATER, 0.05, 0.8, 0.1, 0.1,
            BinomialEffectScale.RISK_DIFFERENCE, True,
        ).validate()
        result_rd = plan_binomial(spec_rd, "run-rd", "fp", logger)
        assert result_rd.is_committed()

        spec_rr = BinomialSpec(
            Alternative.GREATER, 0.05, 0.8, 0.1, 2.0,
            BinomialEffectScale.RELATIVE_RISK, True,
        ).validate()
        assert spec_rr.alternative_p1 == pytest.approx(0.2)
        result_rr = plan_binomial(spec_rr, "run-rr", "fp", logger)
        assert result_rr.is_committed()
