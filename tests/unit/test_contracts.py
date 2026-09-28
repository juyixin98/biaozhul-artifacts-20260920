"""Contract validation tests: explicit parameters and closed failure states."""
from __future__ import annotations

import pytest

from sample_size_planner.contracts import (
    BinomialSpec,
    FailureCategory,
    NormalSpec,
    TestDirection,
)


@pytest.mark.unit
def test_alpha_outside_unit_interval_rejected() -> None:
    with pytest.raises(ValueError):
        NormalSpec(alpha=1.0, power=0.8, direction=TestDirection.TWO_SIDED,
                   standardized_effect=0.5)
    with pytest.raises(ValueError):
        NormalSpec(alpha=0.0, power=0.8, direction=TestDirection.TWO_SIDED,
                   standardized_effect=0.5)


@pytest.mark.unit
def test_non_finite_inputs_rejected() -> None:
    with pytest.raises(ValueError):
        NormalSpec(alpha=float("nan"), power=0.8, direction=TestDirection.TWO_SIDED,
                   standardized_effect=0.5)


@pytest.mark.unit
def test_power_must_exceed_alpha() -> None:
    with pytest.raises(ValueError, match="power"):
        BinomialSpec(p0=0.5, p1=0.7, alpha=0.05, power=0.05,
                     direction=TestDirection.TWO_SIDED)


@pytest.mark.unit
def test_allocation_ratio_must_be_positive() -> None:
    with pytest.raises(ValueError):
        BinomialSpec(p0=0.5, p1=0.7, alpha=0.05, power=0.8,
                     direction=TestDirection.TWO_SIDED, allocation_ratio=0.0)


@pytest.mark.unit
def test_zero_effect_normal_is_a_contract_but_solvable_as_failure() -> None:
    spec = NormalSpec(alpha=0.05, power=0.8, direction=TestDirection.TWO_SIDED,
                      standardized_effect=0.0)
    assert spec.effect_zero is True


@pytest.mark.unit
def test_zero_effect_binomial_two_sided_permitted_but_flagged() -> None:
    spec = BinomialSpec(p0=0.4, p1=0.4, alpha=0.05, power=0.8,
                        direction=TestDirection.TWO_SIDED)
    assert spec.effect_zero is True


@pytest.mark.unit
def test_failure_category_is_closed_enum() -> None:
    # unknown/exception states are named categories, never a generic success
    known = {c.value for c in FailureCategory}
    assert {"effect_zero", "approximation_unreliable_low_rate",
            "exact_limited_by_cap", "interim_look_outside_commitment"} <= known
    assert FailureCategory.NONE.value == "none"
