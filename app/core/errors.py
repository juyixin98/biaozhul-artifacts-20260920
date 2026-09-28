"""Error bounds, high-precision references and verdict adjudication.

The reference sum is *never* produced by the kernels under test.  Two
independent oracles are used:

* ``mpmath`` arbitrary precision (default 80 decimal digits) for inputs up
  to the configured size - it sums the exact binary64 values, so it measures
  pure floating-point rounding error;
* 80-bit extended precision (``numpy.longdouble`` on x86) pairwise
  summation for very large inputs, with an explicitly reported uncertainty.

When the oracle's own uncertainty is not comfortably below the observed
error, the verdict is :attr:`Verdict.INCONCLUSIVE` rather than a rubber stamp.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

import mpmath

from ..config import MACHINE_EPSILON, settings

#: Binary64 rounding unit.
U = MACHINE_EPSILON


class Verdict(str, Enum):
    """Adjudication of a computed sum against theory and the oracle."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class ReferenceMethod(str, Enum):
    MPMATH = "mpmath"
    LONGDOUBLE = "longdouble_pairwise"


@dataclass(frozen=True)
class ReferenceValue:
    """An oracle sum with a conservative self-uncertainty estimate."""

    value: float
    method: ReferenceMethod
    precision_digits: int
    abs_uncertainty: float

    def to_plain(self) -> dict:
        return {
            "value": _json_float(self.value),
            "method": self.method.value,
            "precision_decimal_digits": self.precision_digits,
            "estimated_absolute_uncertainty": _json_float(self.abs_uncertainty),
        }


@dataclass(frozen=True)
class ErrorAssessment:
    """Result of comparing one method's output with the reference."""

    method: str
    result: float
    abs_error: float
    rel_error: float | None
    theoretical_bound: float | None
    bound_ratio: float | None
    condition_number: float | None
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)

    def to_plain(self) -> dict:
        return {
            "method": self.method,
            "result": _json_float(self.result),
            "abs_error": _json_float(self.abs_error),
            "rel_error": _json_float(self.rel_error),
            "theoretical_abs_bound": _json_float(self.theoretical_bound),
            "observed_over_bound_ratio": _json_float(self.bound_ratio),
            "condition_number_sumabs_over_abssum": _json_float(self.condition_number),
            "verdict": self.verdict.value,
            "reasons": list(self.reasons),
        }


# ---------------------------------------------------------------------------
# Theoretical a-priori bounds (Higham, Accuracy and Stability of Numerical
# Algorithms, 2nd ed., ch. 4):  |e| <= gamma_k * sum|x_i|,
# gamma_k = k*u / (1 - k*u).
# ---------------------------------------------------------------------------

def gamma(k: int) -> float:
    """Worst-case growth factor for k serial binary64 additions."""
    ku = k * U
    if ku >= 1.0:
        return math.inf
    return ku / (1.0 - ku)


def naive_bound(n: int, sum_abs: float) -> float:
    """Classical recursive-summation bound: gamma_{n-1} * sum|x_i|."""
    if n <= 1:
        return 0.0
    return gamma(n - 1) * sum_abs


def pairwise_bound(n: int, leaf_width: int, sum_abs: float) -> float:
    """Pairwise tree: leaf accumulation plus ceil(log2) merge levels."""
    if n <= 1:
        return 0.0
    n_blocks = max(1, math.ceil(n / leaf_width))
    depth = max(1, math.ceil(math.log2(n_blocks))) if n_blocks > 1 else 0
    leaf_terms = min(n, leaf_width) - 1
    return (gamma(leaf_terms) + gamma(depth)) * sum_abs


def kahan_bound(n: int, sum_abs: float) -> float:
    """Kahan compensated bound: (2u + n*u^2)/(1 - 2u) * sum|x_i|.

    The ``n*u**2`` second-order term is the standard refined estimate;
    unlike the naive gamma bound this stays O(u) instead of O(n*u).
    """
    if n <= 1:
        return 0.0
    numerator = 2.0 * U + n * U * U
    return (numerator / (1.0 - 2.0 * U)) * sum_abs


# ---------------------------------------------------------------------------
# Independent reference oracles.
# ---------------------------------------------------------------------------

def mpmath_reference(values: np.ndarray, precision_digits: int) -> ReferenceValue:
    """Arbitrary-precision sum of the *exact* binary64 input values."""
    old_dps = mpmath.mp.dps
    mpmath.mp.dps = precision_digits
    try:
        total = mpmath.mpf(0)
        # mpmath.mpf(python_float) reads the exact binary64 significand.
        for x in values.tolist():
            total += mpmath.mpf(x)
        value = float(total)
        # Conservative: n additions, each can lose ~1 unit at dps digits,
        # scaled by the magnitude of the total itself.
        abs_uncertainty = float(
            values.size
            * mpmath.mpf(10) ** (-precision_digits)
            * max(mpmath.mpf(1), mpmath.fabs(total))
        )
    finally:
        mpmath.mp.dps = old_dps
    return ReferenceValue(
        value=value,
        method=ReferenceMethod.MPMATH,
        precision_digits=precision_digits,
        abs_uncertainty=abs_uncertainty,
    )


def longdouble_pairwise_reference(values: np.ndarray) -> ReferenceValue:
    """Extended-precision oracle (x86 80-bit: ~18 significant decimal digits)."""
    ld = values.astype(np.longdouble)
    # Balanced reduction entirely in long double: small sequential leaves
    # (64 terms) merged in a balanced tree, so only log-depth error accrues
    # rather than a full O(block) sequential term.
    leaf = 64
    parts: list[np.longdouble] = []
    for i in range(0, ld.size, leaf):
        acc = np.longdouble(0)
        for v in ld[i : i + leaf].tolist():
            acc += v
        parts.append(acc)
    while len(parts) > 1:
        parts = [parts[i] + parts[i + 1] for i in range(0, len(parts) - 1, 2)] + (
            [parts[-1]] if len(parts) % 2 else []
        )
    ref = float(parts[0]) if parts else 0.0
    eps_ld = float(np.finfo(np.longdouble).eps)
    n_leaves = max(1, math.ceil(values.size / leaf))
    depth = math.ceil(math.log2(n_leaves)) if n_leaves > 1 else 0
    # Leaf accumulation (leaf-1 adds) plus depth balanced merge levels,
    # all measured in long-double ulps and scaled by sum|x|.
    abs_uncertainty = ((leaf - 1) + depth) * eps_ld * float(np.sum(np.abs(ld)))
    return ReferenceValue(
        value=ref,
        method=ReferenceMethod.LONGDOUBLE,
        precision_digits=int(np.finfo(np.longdouble).precision),
        abs_uncertainty=abs_uncertainty,
    )


def reference_sum(values: np.ndarray) -> ReferenceValue:
    """Pick the strongest oracle affordable for the input size."""
    if values.size <= settings.reference_max_length:
        return mpmath_reference(values, settings.reference_precision)
    return longdouble_pairwise_reference(values)


# ---------------------------------------------------------------------------
# Adjudication.
# ---------------------------------------------------------------------------

def assess_error(
    method: str,
    result: float,
    reference: ReferenceValue,
    n: int,
    sum_abs: float,
    bound: float | None,
    tolerance_factor: float | None = None,
) -> ErrorAssessment:
    """Compare ``result`` with the oracle and adjudicate against the bound."""
    factor = settings.error_tolerance_factor if tolerance_factor is None else tolerance_factor
    reasons: list[str] = []
    ref = reference.value
    abs_error = abs(result - ref)

    if not math.isfinite(abs_error):
        return ErrorAssessment(
            method=method, result=result, abs_error=math.nan, rel_error=None,
            theoretical_bound=bound, bound_ratio=None, condition_number=None,
            verdict=Verdict.INCONCLUSIVE,
            reasons=["non-finite result compared to finite reference; special-value policy applies"],
        )

    rel_error: float | None = None
    condition: float | None = None
    if ref != 0.0:
        rel_error = abs_error / abs(ref)
        condition = sum_abs / abs(ref)

    bound_ratio = abs_error / bound if bound and math.isfinite(bound) and bound > 0 else None

    if bound is None or not math.isfinite(bound):
        return ErrorAssessment(
            method=method, result=result, abs_error=abs_error, rel_error=rel_error,
            theoretical_bound=bound, bound_ratio=bound_ratio, condition_number=condition,
            verdict=Verdict.INCONCLUSIVE,
            reasons=["no finite theoretical bound at this length; reporting measured error only"],
        )

    threshold = factor * bound
    uncertainty = reference.abs_uncertainty
    # The true error can lie anywhere in this interval given oracle noise.
    error_low = max(0.0, abs_error - uncertainty)
    error_high = abs_error + uncertainty

    if error_high <= threshold:
        verdict = Verdict.ACCEPTED
        reasons.append(
            f"absolute error {abs_error:.3e} (+/- oracle {uncertainty:.2e}) is within "
            f"{factor:g}x the a-priori bound {bound:.3e}"
        )
    elif error_low > threshold:
        verdict = Verdict.REJECTED
        reasons.append(
            f"absolute error {abs_error:.3e} (+/- oracle {uncertainty:.2e}) exceeds "
            f"{factor:g}x the a-priori bound {bound:.3e} even accounting for oracle noise"
        )
    else:
        verdict = Verdict.INCONCLUSIVE
        reasons.append(
            f"error interval [{error_low:.3e}, {error_high:.3e}] straddles the adjudication "
            f"threshold {threshold:.3e} ({factor:g}x bound {bound:.3e}); oracle precision "
            f"{uncertainty:.2e} is insufficient to accept or reject reliably"
        )

    if condition is not None and condition > 1.0 / U:
        reasons.append(
            f"severe cancellation: condition number {condition:.3e} approaches 1/u; "
            "no algorithm can recover digits absent from the inputs"
        )

    return ErrorAssessment(
        method=method, result=result, abs_error=abs_error, rel_error=rel_error,
        theoretical_bound=bound, bound_ratio=bound_ratio, condition_number=condition,
        verdict=verdict, reasons=reasons,
    )


def _json_float(value: float | None) -> float | str | None:
    """Render floats JSON-safe, preserving NaN/Inf as tagged strings."""
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
    return value
