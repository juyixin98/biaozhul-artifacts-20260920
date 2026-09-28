"""Unit tests for the statistical contract layer."""
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
from ssp.errors import ValidationError


def normal_spec(**overrides) -> NormalSpec:
    base = dict(
        alternative=Alternative.TWO_SIDED,
        alpha=0.05,
        target_power=0.8,
        effect=0.5,
        effect_scale=NormalEffectScale.STANDARDIZED_D,
        two_sample=False,
    )
    base.update(overrides)
    return NormalSpec(**base)


def binomial_spec(**overrides) -> BinomialSpec:
    base = dict(
        alternative=Alternative.GREATER,
        alpha=0.05,
        target_power=0.8,
        p0=0.1,
        effect=0.2,
        effect_scale=BinomialEffectScale.PROPORTIONS,
        two_sample=False,
    )
    base.update(overrides)
    return BinomialSpec(**base)


class TestNormalContract:
    @pytest.mark.unit
    def test_accepts_valid_spec_and_exposes_direction(self):
        spec = normal_spec().validate()
        assert spec.alternative is Alternative.TWO_SIDED
        assert spec.alternative.tail_probability == 0.5

    @pytest.mark.unit
    @pytest.mark.parametrize("direction", [Alternative.GREATER, Alternative.LESS])
    def test_one_sided_tail_mass_is_one(self, direction):
        spec = normal_spec(alternative=direction).validate()
        assert spec.alternative.tail_probability == 1.0

    @pytest.mark.unit
    @pytest.mark.parametrize("alpha", [0.0, 1.0, -0.01, 1.01])
    def test_alpha_must_be_strictly_interior(self, alpha):
        with pytest.raises(ValidationError) as exc:
            normal_spec(alpha=alpha).validate()
        assert exc.value.category.value == "validation_error"

    @pytest.mark.unit
    def test_power_must_exceed_alpha(self):
        with pytest.raises(ValidationError, match="target_power must exceed alpha"):
            normal_spec(alpha=0.2, target_power=0.2).validate()

    @pytest.mark.unit
    def test_zero_effect_is_rejected_not_planned(self):
        with pytest.raises(ValidationError) as exc:
            normal_spec(effect=0.0).validate()
        assert "exactly zero" in str(exc.value)

    @pytest.mark.unit
    def test_non_finite_effect_rejected(self):
        with pytest.raises(ValidationError):
            normal_spec(effect=float("nan")).validate()

    @pytest.mark.unit
    def test_absolute_scale_requires_sigma(self):
        spec = normal_spec(
            effect=2.0, effect_scale=NormalEffectScale.ABSOLUTE_DIFFERENCE, sigma=None
        )
        with pytest.raises(ValidationError, match="sigma is required"):
            spec.validate()

    @pytest.mark.unit
    def test_standardized_scale_rejects_sigma(self):
        spec = normal_spec(effect_scale=NormalEffectScale.STANDARDIZED_D, sigma=2.0)
        with pytest.raises(ValidationError, match="sigma"):
            spec.validate()

    @pytest.mark.unit
    def test_standardized_effect_conversion(self):
        spec = normal_spec(
            effect=1.5, effect_scale=NormalEffectScale.ABSOLUTE_DIFFERENCE, sigma=3.0
        ).validate()
        assert spec.standardized_effect == pytest.approx(0.5)

    @pytest.mark.unit
    @pytest.mark.parametrize("ratio", [0.0, -1.0])
    def test_allocation_ratio_must_be_positive(self, ratio):
        with pytest.raises(ValidationError, match="allocation_ratio"):
            normal_spec(two_sample=True, allocation_ratio=ratio).validate()

    @pytest.mark.unit
    def test_interim_peeking_is_outside_the_commitment(self):
        with pytest.raises(ValidationError, match="interim peeking"):
            normal_spec(interim_looks=3).validate()


class TestBinomialContract:
    @pytest.mark.unit
    def test_proportions_scale_passthrough(self):
        spec = binomial_spec(p0=0.1, effect=0.2).validate()
        assert spec.alternative_p1 == pytest.approx(0.2)

    @pytest.mark.unit
    def test_risk_difference_scale(self):
        spec = binomial_spec(
            p0=0.1, effect=0.05, effect_scale=BinomialEffectScale.RISK_DIFFERENCE
        ).validate()
        assert spec.alternative_p1 == pytest.approx(0.15)

    @pytest.mark.unit
    def test_relative_risk_scale(self):
        spec = binomial_spec(
            p0=0.1, effect=2.0, effect_scale=BinomialEffectScale.RELATIVE_RISK
        ).validate()
        assert spec.alternative_p1 == pytest.approx(0.2)

    @pytest.mark.unit
    def test_odds_ratio_scale(self):
        # OR = (p1/(1-p1)) / (p0/(1-p0)); OR=3 with p0=0.1 -> p1=0.25
        spec = binomial_spec(
            p0=0.1, effect=3.0, effect_scale=BinomialEffectScale.ODDS_RATIO
        ).validate()
        assert spec.alternative_p1 == pytest.approx(0.25)

    @pytest.mark.unit
    def test_direction_must_agree_with_effect_greater(self):
        spec = binomial_spec(alternative=Alternative.LESS, p0=0.2, effect=0.3)
        with pytest.raises(ValidationError, match="less requires p1 < p0"):
            spec.validate()

    @pytest.mark.unit
    def test_direction_must_agree_with_effect_less(self):
        spec = binomial_spec(alternative=Alternative.GREATER, p0=0.3, effect=0.2)
        with pytest.raises(ValidationError, match="greater requires p1 > p0"):
            spec.validate()

    @pytest.mark.unit
    def test_derived_p1_outside_unit_interval_rejected(self):
        spec = binomial_spec(
            p0=0.8, effect=0.5, effect_scale=BinomialEffectScale.RISK_DIFFERENCE
        )
        with pytest.raises(ValidationError, match="0 < p1 < 1"):
            spec.validate()

    @pytest.mark.unit
    def test_p0_boundary_rejected(self):
        with pytest.raises(ValidationError):
            binomial_spec(p0=0.0).validate()

    @pytest.mark.unit
    def test_interim_peeking_rejected(self):
        with pytest.raises(ValidationError, match="interim peeking"):
            binomial_spec(interim_looks=2).validate()

    @pytest.mark.unit
    def test_method_preference_is_explicit(self):
        spec = binomial_spec(method_preference=MethodPreference.EXACT).validate()
        assert spec.method_preference is MethodPreference.EXACT
