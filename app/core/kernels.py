"""Numerical summation kernels.

Three classic finite-input summation rules are implemented explicitly:

* :func:`naive_sum`   - left-to-right recursive summation, ``s := s + x_i``.
* :func:`kahan_sum`   - Kahan compensated summation (state ``(sum, compensation)``).
* :func:`pairwise_sum`- divide-and-conquer tree summation with sequential
  leaves (:data:`LEAF_SIZE` elements per leaf).

The chunked / streaming merge algorithms that carry compensation state across
blocks live in :mod:`app.core.chunking`; this module is deliberately limited
to the three reference kernels.

IEEE-754 special-value policy (fixed for the whole service):

* any NaN in the input                  -> quiet positive NaN;
* both ``+inf`` and ``-inf`` present    -> quiet NaN (invalid operation);
* only ``+inf`` / only ``-inf`` present -> signed infinity;
* empty input, or a result equal to zero:
    - empty set                          -> ``+0.0``;
    - only negative zero elements        -> ``-0.0``;
    - every other zero case              -> ``+0.0``
      (mixed zero signs, or non-zero elements that cancel exactly).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# Pairwise tree leaves: below this many elements a subtree is summed
# left-to-right.  32 keeps leaf depth bounded without micro-optimisation.
LEAF_SIZE = 32


@dataclass(frozen=True)
class SpecialAssessment:
    """Census of IEEE special values in a sequence.

    The census is order-independent, which is exactly why rearrangement can
    never change the service's special-value verdict.
    """

    has_nan: bool
    pos_inf: int
    neg_inf: int
    nonzero_finite: int
    pos_zero: int
    neg_zero: int

    @property
    def n_elements(self) -> int:
        return (
            int(self.has_nan)
            + self.pos_inf
            + self.neg_inf
            + self.nonzero_finite
            + self.pos_zero
            + self.neg_zero
        )


def assess(values: np.ndarray) -> SpecialAssessment:
    """Count NaNs, infinities and signed zeros in ``values`` (float64)."""
    total = values.size
    finite = np.isfinite(values)
    n_finite = int(finite.sum())
    n_nan = total - n_finite - int(np.isinf(values).sum())

    finite_vals = values[finite]
    zeros = finite_vals == 0.0
    n_zero = int(zeros.sum())
    # signbit(-0.0) is True; signbit(+0.0) is False.
    n_neg_zero = int(np.signbit(finite_vals[zeros]).sum()) if n_zero else 0
    n_pos_zero = n_zero - n_neg_zero

    sign_of_inf = np.sign(values[np.isinf(values)]) if total - n_finite - n_nan else np.array([])
    n_pos_inf = int((sign_of_inf > 0).sum())
    n_neg_inf = int((sign_of_inf < 0).sum())

    return SpecialAssessment(
        has_nan=bool(n_nan),
        pos_inf=n_pos_inf,
        neg_inf=n_neg_inf,
        nonzero_finite=n_finite - n_zero,
        pos_zero=n_pos_zero,
        neg_zero=n_neg_zero,
    )


def signed_zero_for(assessment: SpecialAssessment) -> float:
    """Return the fixed-policy signed zero for an all-zero/empty finite sum."""
    if (
        assessment.nonzero_finite == 0
        and assessment.neg_zero > 0
        and assessment.pos_zero == 0
    ):
        return -0.0
    return 0.0


def resolve_special(assessment: SpecialAssessment) -> float | None:
    """Apply the fixed NaN/infinity policy.

    Returns the mandated float result, or ``None`` when the sequence is
    purely finite and must be summed by a real kernel.
    """
    if assessment.has_nan:
        return math.nan
    if assessment.pos_inf and assessment.neg_inf:
        return math.nan
    if assessment.pos_inf:
        return math.inf
    if assessment.neg_inf:
        return -math.inf
    return None


def _canonicalize_zero(result: float, assessment: SpecialAssessment) -> float:
    if result == 0.0:
        return signed_zero_for(assessment)
    return result


def naive_sum(values: np.ndarray, assessment: SpecialAssessment | None = None) -> float:
    """Strict left-to-right summation, one ``float64`` add per element."""
    info = assessment if assessment is not None else assess(values)
    total = 0.0
    # ``tolist()`` boxes into Python floats; the loop below performs exact
    # IEEE-754 round-to-nearest adds in program order (no pairwise/SIMD
    # reassociation the way NumPy reduction kernels may apply).
    for x in values.tolist():
        total += x
    return _canonicalize_zero(total, info)


def kahan_sum(values: np.ndarray, assessment: SpecialAssessment | None = None) -> float:
    """Classic Kahan compensated summation.

    ``s`` is the running sum and ``c`` the running compensation; the pair
    ``(s, c)`` is exactly the state that must be carried across chunks
    (see :class:`app.core.chunking.StreamingKahan`).
    """
    info = assessment if assessment is not None else assess(values)
    s = 0.0
    c = 0.0
    for x in values.tolist():
        y = x - c
        t = s + y
        c = (t - s) - y
        s = t
    return _canonicalize_zero(s + c, info)


def _sequential(values: np.ndarray, lo: int, hi: int) -> float:
    total = 0.0
    for i in range(lo, hi):
        total += float(values[i])
    return total


def _pairwise_recursive(values: np.ndarray, lo: int, hi: int) -> float:
    if hi - lo <= LEAF_SIZE:
        return _sequential(values, lo, hi)
    mid = (lo + hi) // 2
    return _pairwise_recursive(values, lo, mid) + _pairwise_recursive(values, mid, hi)


def pairwise_sum(values: np.ndarray, assessment: SpecialAssessment | None = None) -> float:
    """Balanced divide-and-conquer summation with sequential leaves."""
    info = assessment if assessment is not None else assess(values)
    if values.size == 0:
        return _canonicalize_zero(0.0, info)
    result = _pairwise_recursive(values, 0, values.size)
    return _canonicalize_zero(result, info)
