"""Statistical contracts: explicit enums, validated parameters and result types.

Every quantity that changes the meaning of a sample-size calculation is an
explicit field here rather than an implicit convention:

* test direction (two-sided / greater / less)
* allocation ratio between groups
* significance level, target power, effect scale
* a *closed* failure-category enum so an unknown state is never reported as
  success.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
# Explicit statistical enums
# --------------------------------------------------------------------------- #
class TestDirection(str, enum.Enum):
    TWO_SIDED = "two_sided"
    GREATER = "greater"
    LESS = "less"

    @property
    def is_two_sided(self) -> bool:
        return self is TestDirection.TWO_SIDED

    # prevent pytest from collecting this statistical enum as a test class
    __test__ = False


class SolverMethod(str, enum.Enum):
    """How the operating characteristic was evaluated."""

    NORMAL_APPROX = "normal_approx"          # non-central normal analytic power
    EXACT_BINOMIAL = "exact_binomial"        # exact binomial / Fisher power
    STUDENT_T = "student_t"                  # non-central t (unknown variance)


class FailureCategory(str, enum.Enum):
    """Closed set of non-success outcomes. Never collapse these into success."""

    NONE = "none"
    INVALID_INPUT = "invalid_input"
    EFFECT_ZERO = "effect_zero"                    # power == alpha, no finite n
    APPROXIMATION_UNRELIABLE_LOW_RATE = "approximation_unreliable_low_rate"
    EXACT_LIMITED_BY_CAP = "exact_limited_by_cap"  # exact search hit max n
    NON_CONVERGENCE = "non_convergence"
    COMPUTATION_ERROR = "computation_error"
    INTERIM_LOOK_OUTSIDE_COMMITMENT = "interim_look_outside_commitment"
    TOO_MANY_INTERIM_LOOKS = "too_many_interim_looks"


class EstimandFamily(str, enum.Enum):
    NORMAL = "normal"       # composite normal means (z / t)
    BINOMIAL = "binomial"   # two proportions (or one proportion vs reference)


class EffectScale(str, enum.Enum):
    DIFFERENCE = "difference"      # p1 - p0 or mu1 - mu0 on raw scale
    STANDARDIZED = "standardized"  # Cohen's d


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _finite_number(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return float(value)


def _finite_positive(name: str, value: float) -> float:
    value = _finite_number(name, value)
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")
    return value


def _bounded_probability(name: str, value: float) -> float:
    value = _finite_number(name, value)
    if not 0.0 < value < 1.0:
        raise ValueError(f"{name} must lie strictly in (0, 1), got {value}")
    return value


# --------------------------------------------------------------------------- #
# Validated parameter objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NormalSpec:
    """Contract for a composite normal-mean comparison.

    Either ``standardized_effect`` is supplied (Cohen's d, unit variance) or a
    raw ``delta`` with the group standard deviations. ``standardized_effect``
    is allowed to be exactly 0: the solver then returns the explicit
    ``EFFECT_ZERO`` failure instead of pretending a finite n exists.
    """

    alpha: float
    power: float
    direction: TestDirection
    allocation_ratio: float = 1.0          # n1 / n0
    one_sample: bool = False
    standardized_effect: Optional[float] = None
    delta: Optional[float] = None          # mu1 - mu0 on raw scale
    sd0: float = 1.0
    sd1: Optional[float] = None            # defaults to sd0 (equal-variance case)
    use_t_distribution: bool = False

    def __post_init__(self) -> None:
        _bounded_probability("alpha", self.alpha)
        _bounded_probability("power", self.power)
        if self.power <= self.alpha:
            raise ValueError("target power must exceed alpha")
        _finite_positive("allocation_ratio", self.allocation_ratio)
        _finite_positive("sd0", self.sd0)
        if self.sd1 is not None:
            _finite_positive("sd1", self.sd1)

        if self.standardized_effect is not None:
            _finite_number("standardized_effect", self.standardized_effect)
            if abs(self.standardized_effect) > 10.0:
                raise ValueError("standardized_effect outside sane range (-10, 10)")
        elif self.delta is not None:
            _finite_number("delta", self.delta)
        else:
            raise ValueError("provide either standardized_effect or delta")

    @property
    def effect_raw(self) -> float:
        """Absolute effect on the raw (mean) scale."""
        if self.standardized_effect is not None:
            return abs(self.standardized_effect) * self.sd0
        return abs(float(self.delta))

    @property
    def effect_zero(self) -> bool:
        if self.standardized_effect is not None:
            return self.standardized_effect == 0.0
        return float(self.delta) == 0.0

    @property
    def sd1_effective(self) -> float:
        return self.sd0 if self.sd1 is None else self.sd1


@dataclass(frozen=True)
class BinomialSpec:
    """Contract for a proportion comparison.

    ``one_sample=True`` tests one Binomial arm against a fixed reference
    ``p0``. Otherwise two independent arms with ``n1 / n0 = allocation_ratio``.
    ``p1 == p0`` is permitted for a two-sided design and surfaces as the
    explicit ``EFFECT_ZERO`` failure category.
    """

    p0: float
    p1: float
    alpha: float
    power: float
    direction: TestDirection
    allocation_ratio: float = 1.0
    one_sample: bool = False
    scale: EffectScale = EffectScale.DIFFERENCE
    continuity_correction: bool = False

    def __post_init__(self) -> None:
        _bounded_probability("p0", self.p0)
        _bounded_probability("p1", self.p1)
        _bounded_probability("alpha", self.alpha)
        _bounded_probability("power", self.power)
        if self.power <= self.alpha:
            raise ValueError("target power must exceed alpha")
        _finite_positive("allocation_ratio", self.allocation_ratio)

        if self.direction is TestDirection.GREATER and self.p1 <= self.p0:
            raise ValueError("direction 'greater' requires p1 > p0 (otherwise power cannot exceed alpha)")
        if self.direction is TestDirection.LESS and self.p1 >= self.p0:
            raise ValueError("direction 'less' requires p1 < p0 (otherwise power cannot exceed alpha)")

    @property
    def effect_on_alternative(self) -> float:
        return abs(self.p1 - self.p0)

    @property
    def effect_zero(self) -> bool:
        return self.p0 == self.p1


# --------------------------------------------------------------------------- #
# Result contract
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SampleSizeResult:
    success: bool
    family: EstimandFamily
    n_per_group0: Optional[int] = None       # control / reference arm
    n_per_group1: Optional[int] = None       # treatment arm (None if one-sample)
    n_total: Optional[int] = None
    achieved_power: Optional[float] = None
    method: Optional[SolverMethod] = None
    failure_category: FailureCategory = FailureCategory.NONE
    failure_detail: Optional[str] = None
    power_at_n_minus_one: Optional[float] = None     # boundary witness
    approximation_discrepancy: Optional[float] = None
    diagnostics: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.success and self.failure_category is FailureCategory.NONE
