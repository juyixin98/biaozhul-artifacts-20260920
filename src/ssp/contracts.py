"""Statistical contract types.

Everything that defines a planning request is explicit here:

* test direction            - :class:`Alternative`
* allocation ratio          - ``allocation_ratio`` (arm1 / arm0)
* significance / target     - ``alpha`` / ``target_power``
* effect scale              - :class:`NormalEffectScale` / :class:`BinomialEffectScale`
* analysis commitment       - exactly one look (interim peeking is rejected;
  repeated interim analyses are outside the fixed-sample commitment)

Core contracts are plain immutable dataclasses with explicit ``validate``
methods; the web layer maps them to/from pydantic models.  Keeping the core
free of web types lets the kernels and the independent tests import the same
contract without a running application.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import ValidationError

_ALPHA_BOUNDS = (0.0, 1.0)
_POWER_BOUNDS = (0.0, 1.0)
_PROPORTION_BOUNDS = (0.0, 1.0)


class Alternative(str, Enum):
    """Test direction."""

    TWO_SIDED = "two_sided"
    GREATER = "greater"
    LESS = "less"

    @property
    def tail_probability(self) -> float:
        """Alpha mass in the tested tail(s)."""
        return 0.5 if self is Alternative.TWO_SIDED else 1.0


class NormalEffectScale(str, Enum):
    """Effect scale for normal endpoints."""

    ABSOLUTE_DIFFERENCE = "absolute_difference"
    """Raw mean difference ``mu1 - mu0``; ``sigma`` must be supplied."""

    STANDARDIZED_D = "standardized_d"
    """Cohen's d; sigma is implicitly 1."""


class BinomialEffectScale(str, Enum):
    """Effect scale for binomial endpoints.

    ``p0`` is always the control/null proportion; ``p1`` under the alternative
    is derived from the scale.
    """

    RISK_DIFFERENCE = "risk_difference"
    """``p1 = p0 + effect`` (absolute percentage-point difference)."""

    RELATIVE_RISK = "relative_risk"
    """``p1 = p0 * effect``."""

    ODDS_RATIO = "odds_ratio"
    """``p1 = or * p0 / (1 - p0 + or * p0)``."""

    PROPORTIONS = "proportions"
    """``effect`` directly gives ``p1``."""


class MethodPreference(str, Enum):
    """How the planner should choose its computation method."""

    AUTO = "auto"
    """Asymptotic when expected counts are adequate, exact otherwise."""

    ASYMPTOTIC = "asymptotic"
    """Force the normal approximation (never silently downgrade)."""

    EXACT = "exact"
    """Force the exact (binomial / Fisher) computation."""


def _check_finite(name: str, value: float) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValidationError(f"{name} must be a finite number", details={"name": name, "value": value})


def _check_in_range(name: str, value: float, low: float, high: float, *, strict: bool = True) -> None:
    _check_finite(name, value)
    v = float(value)
    if strict:
        if not (low < v < high):
            raise ValidationError(
                f"{name} must satisfy {low} < {name} < {high}", details={"name": name, "value": v}
            )
    else:
        if not (low <= v <= high):
            raise ValidationError(
                f"{name} must satisfy {low} <= {name} <= {high}", details={"name": name, "value": v}
            )


def _check_positive(name: str, value: float) -> None:
    _check_finite(name, value)
    if float(value) <= 0:
        raise ValidationError(f"{name} must be > 0", details={"name": name, "value": float(value)})


def _check_positive_int(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{name} must be a positive integer", details={"name": name, "value": value})


@dataclass(frozen=True)
class NormalSpec:
    """Contract for a normal-endpoint sample size plan."""

    alternative: Alternative
    alpha: float
    target_power: float
    effect: float
    effect_scale: NormalEffectScale
    two_sample: bool
    sigma: float | None = None
    known_sigma: bool = False
    allocation_ratio: float = 1.0
    interim_looks: int = 1
    method_preference: MethodPreference = MethodPreference.AUTO

    def validate(self) -> "NormalSpec":
        if not isinstance(self.alternative, Alternative):
            raise ValidationError("alternative must be one of two_sided/greater/less")
        if not isinstance(self.effect_scale, NormalEffectScale):
            raise ValidationError("effect_scale must be absolute_difference or standardized_d")
        _check_in_range("alpha", self.alpha, *_ALPHA_BOUNDS)
        _check_in_range("target_power", self.target_power, *_POWER_BOUNDS)
        if self.target_power <= self.alpha:
            raise ValidationError(
                "target_power must exceed alpha (a test cannot beat its size under the null)",
                details={"alpha": self.alpha, "target_power": self.target_power},
            )
        _check_finite("effect", self.effect)
        if self.effect == 0.0:
            raise ValidationError(
                "effect is exactly zero; no finite sample can exceed alpha under the null",
                details={"effect": 0.0},
            )
        if self.effect_scale is NormalEffectScale.ABSOLUTE_DIFFERENCE:
            if self.sigma is None:
                raise ValidationError("sigma is required for effect_scale=absolute_difference")
            _check_positive("sigma", self.sigma)
        elif self.sigma is not None and self.sigma != 1.0:
            raise ValidationError(
                "sigma must be omitted (or 1) for effect_scale=standardized_d",
                details={"sigma": self.sigma},
            )
        _check_positive("allocation_ratio", self.allocation_ratio)
        _check_positive_int("interim_looks", self.interim_looks)
        if self.interim_looks > 1:
            raise ValidationError(
                "interim peeking is outside the fixed-sample commitment; "
                "this planner prices a single pre-specified test",
                details={"interim_looks": self.interim_looks},
            )
        if not isinstance(self.method_preference, MethodPreference):
            raise ValidationError("method_preference must be auto/asymptotic/exact")
        # Normal endpoint has no exact discrete counterpart; exact is a normal
        # request only when sigma is known (z). Unknown sigma uses noncentral t.
        return self

    @property
    def standardized_effect(self) -> float:
        """Cohen's d (sign preserved; power depends on its magnitude)."""
        if self.effect_scale is NormalEffectScale.STANDARDIZED_D:
            return float(self.effect)
        return float(self.effect) / float(self.sigma)

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": "normal",
            "alternative": self.alternative.value,
            "alpha": self.alpha,
            "target_power": self.target_power,
            "effect": self.effect,
            "effect_scale": self.effect_scale.value,
            "two_sample": self.two_sample,
            "sigma": self.sigma,
            "known_sigma": self.known_sigma,
            "allocation_ratio": self.allocation_ratio,
            "interim_looks": self.interim_looks,
            "method_preference": self.method_preference.value,
        }


@dataclass(frozen=True)
class BinomialSpec:
    """Contract for a binomial-endpoint sample size plan."""

    alternative: Alternative
    alpha: float
    target_power: float
    p0: float
    effect: float
    effect_scale: BinomialEffectScale
    two_sample: bool
    allocation_ratio: float = 1.0
    interim_looks: int = 1
    method_preference: MethodPreference = MethodPreference.AUTO

    def validate(self) -> "BinomialSpec":
        if not isinstance(self.alternative, Alternative):
            raise ValidationError("alternative must be one of two_sided/greater/less")
        if not isinstance(self.effect_scale, BinomialEffectScale):
            raise ValidationError(
                "effect_scale must be risk_difference/relative_risk/odds_ratio/proportions"
            )
        _check_in_range("alpha", self.alpha, *_ALPHA_BOUNDS)
        _check_in_range("target_power", self.target_power, *_POWER_BOUNDS)
        if self.target_power <= self.alpha:
            raise ValidationError(
                "target_power must exceed alpha",
                details={"alpha": self.alpha, "target_power": self.target_power},
            )
        _check_in_range("p0", self.p0, *_PROPORTION_BOUNDS)
        _check_positive("allocation_ratio", self.allocation_ratio)
        _check_positive_int("interim_looks", self.interim_looks)
        if self.interim_looks > 1:
            raise ValidationError(
                "interim peeking is outside the fixed-sample commitment; "
                "this planner prices a single pre-specified test",
                details={"interim_looks": self.interim_looks},
            )
        if not isinstance(self.method_preference, MethodPreference):
            raise ValidationError("method_preference must be auto/asymptotic/exact")
        p1 = self.alternative_p1
        # Direction must agree with the effect sign.
        if self.alternative is Alternative.GREATER and p1 <= self.p0:
            raise ValidationError(
                "alternative=greater requires p1 > p0", details={"p0": self.p0, "p1": p1}
            )
        if self.alternative is Alternative.LESS and p1 >= self.p0:
            raise ValidationError(
                "alternative=less requires p1 < p0", details={"p0": self.p0, "p1": p1}
            )
        return self

    @property
    def alternative_p1(self) -> float:
        """Derive p1 under the alternative from the declared effect scale."""
        p0 = float(self.p0)
        scale = self.effect_scale
        e = float(self.effect)
        if scale is BinomialEffectScale.PROPORTIONS:
            p1 = e
        elif scale is BinomialEffectScale.RISK_DIFFERENCE:
            p1 = p0 + e
        elif scale is BinomialEffectScale.RELATIVE_RISK:
            _check_positive("effect(relative_risk)", e)
            p1 = p0 * e
        else:  # ODDS_RATIO
            _check_positive("effect(odds_ratio)", e)
            p1 = e * p0 / (1.0 - p0 + e * p0)
        if not (0.0 < p1 < 1.0):
            raise ValidationError(
                "derived alternative proportion p1 must satisfy 0 < p1 < 1",
                details={"p0": p0, "p1": p1, "effect_scale": scale.value},
            )
        return p1

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": "binomial",
            "alternative": self.alternative.value,
            "alpha": self.alpha,
            "target_power": self.target_power,
            "p0": self.p0,
            "effect": self.effect,
            "effect_scale": self.effect_scale.value,
            "two_sample": self.two_sample,
            "allocation_ratio": self.allocation_ratio,
            "interim_looks": self.interim_looks,
            "method_preference": self.method_preference.value,
            "p1": self.alternative_p1,
        }


@dataclass(frozen=True)
class Allocation:
    """Integer arm allocation for a total sample size ``n0 + n1``."""

    n0: int
    n1: int

    @property
    def total(self) -> int:
        return self.n0 + self.n1

    def as_dict(self) -> dict[str, int]:
        return {"n0": self.n0, "n1": self.n1, "total": self.total}


@dataclass(frozen=True)
class PlanResult:
    """The planner's verdict. Failures never produce this object."""

    endpoint: str
    run_id: str
    fingerprint: str
    allocation: Allocation
    achieved_power: float
    target_power: float
    alpha: float
    alternative: Alternative
    method: str
    noncentrality: float | None
    critical_values: tuple[float, ...]
    power_at_total_minus_one: float
    allocation_minus_one: Allocation
    warnings: tuple[str, ...] = field(default_factory=tuple)
    steps: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    spec: dict[str, Any] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)

    def is_committed(self) -> bool:
        """Acceptance predicate: n passes and n-1 does not."""
        return self.achieved_power >= self.target_power and (
            self.power_at_total_minus_one < self.target_power
            or self.allocation_minus_one.total == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "completed",
            "endpoint": self.endpoint,
            "run_id": self.run_id,
            "input_fingerprint": self.fingerprint,
            "target_power": self.target_power,
            "alpha": self.alpha,
            "alternative": self.alternative.value,
            "method": self.method,
            "allocation": self.allocation.as_dict(),
            "achieved_power": self.achieved_power,
            "minimal_integer_check": {
                "passes": self.is_committed(),
                "power_at_total_minus_one": self.power_at_total_minus_one,
                "allocation_minus_one": self.allocation_minus_one.as_dict(),
            },
            "noncentrality": self.noncentrality,
            "critical_values": list(self.critical_values),
            "warnings": list(self.warnings),
            "steps": list(self.steps),
            "spec": self.spec,
            "versions": self.versions,
        }
