"""Tests for error bounds, independent oracles and verdict adjudication.

Reference answers are produced three independent ways - mpmath exact binary64
sum, :func:`math.fsum` (CPython's correctly-rounded Shewchuk algorithm) and
80-bit long double pairwise - and cross-checked against each other.  At no
point is a reference answer produced by the kernel under test.
"""
from __future__ import annotations

import math

import mpmath
import numpy as np
import pytest

from app.core import kernels
from app.core.errors import (
    U,
    Verdict,
    assess_error,
    gamma,
    kahan_bound,
    longdouble_pairwise_reference,
    mpmath_reference,
    naive_bound,
    pairwise_bound,
    reference_sum,
)
from app.services import synthetic


# ---------------------------------------------------------------------------
# Independent oracle cross-validation.
# ---------------------------------------------------------------------------

def fsum_reference(values: np.ndarray) -> float:
    """Second, fully independent oracle: CPython's correctly-rounded fsum."""
    return math.fsum(values.tolist())


@pytest.mark.parametrize("seed", range(20))
def test_mpmath_and_fsum_oracles_agree(seed: int):
    rng = np.random.default_rng(seed)
    values = (rng.uniform(-1, 1, 2000) * 10.0 ** rng.integers(-12, 12, 2000))
    mp = mpmath_reference(values, precision_digits=60).value
    fs = fsum_reference(values)
    # Two independent high-quality oracles must agree to ~1 binary64 ulp.
    assert mp == fs or abs(mp - fs) <= math.ulp(max(abs(mp), abs(fs)))


def test_longdouble_oracle_matches_mpmath_on_small_input():
    values = synthetic.mixed_scales(5000, seed=2)
    mp = mpmath_reference(values, 80).value
    ld = longdouble_pairwise_reference(values).value
    assert abs(ld - mp) <= 2.0 * math.ulp(abs(mp))


def test_oracle_is_not_produced_by_kernel_under_test():
    # Regression guard: the reference must differ from naive on an input
    # where naive is provably wrong.
    values = synthetic.big_cancel(10)
    ref = reference_sum(values).value
    assert ref == 10.0
    assert kernels.naive_sum(values) == 0.0
    assert ref != kernels.naive_sum(values)


# ---------------------------------------------------------------------------
# Theoretical bounds.
# ---------------------------------------------------------------------------

def test_gamma_factor_matches_definition():
    assert gamma(1) == pytest.approx(U / (1 - U))
    assert math.isinf(gamma(2 ** 53 + 1))


@pytest.mark.parametrize("seed", range(40))
def test_naive_never_exceeds_its_a_priori_bound(seed: int):
    rng = np.random.default_rng(seed)
    n = int(1 + rng.integers(2, 2500))
    values = rng.uniform(-1, 1, n) * 10.0 ** rng.integers(-12, 12, n)
    bound = naive_bound(n, float(np.sum(np.abs(values))))
    assert abs(kernels.naive_sum(values) - reference_sum(values).value) <= bound


@pytest.mark.parametrize("seed", range(40))
def test_pairwise_never_exceeds_its_log_depth_bound(seed: int):
    rng = np.random.default_rng(seed)
    n = int(1 + rng.integers(2, 4000))
    values = rng.uniform(-1, 1, n) * 10.0 ** rng.integers(-10, 10, n)
    bound = pairwise_bound(n, kernels.LEAF_SIZE, float(np.sum(np.abs(values))))
    assert abs(kernels.pairwise_sum(values) - reference_sum(values).value) <= bound


@pytest.mark.parametrize("seed", range(40))
def test_kahan_never_exceeds_its_compensated_bound(seed: int):
    rng = np.random.default_rng(seed)
    n = int(1 + rng.integers(2, 4000))
    values = rng.choice(
        [1e15, -1e15, 1.0, -1.0, 1e-9], size=n, p=[0.2, 0.2, 0.2, 0.2, 0.2]
    )
    bound = kahan_bound(n, float(np.sum(np.abs(values))))
    assert abs(kernels.kahan_sum(values) - reference_sum(values).value) <= bound


def test_kahan_bound_is_dramatically_smaller_than_naive_bound_at_scale():
    n = 10_000_000
    sum_abs = 2e16
    assert kahan_bound(n, sum_abs) < 100.0
    assert naive_bound(n, sum_abs) > 2e3


# ---------------------------------------------------------------------------
# Verdicts: concrete acceptance / rejection categories.
# ---------------------------------------------------------------------------

def _ref(value: float, uncertainty: float = 0.0):
    from app.core.errors import ReferenceMethod, ReferenceValue

    return ReferenceValue(
        value=value, method=ReferenceMethod.MPMATH, precision_digits=80,
        abs_uncertainty=uncertainty,
    )


def test_assessor_accepts_error_within_bound():
    assessment = assess_error(
        "x", result=1.01, reference=_ref(1.0), n=100, sum_abs=100.0,
        bound=0.1, tolerance_factor=4.0,
    )
    assert assessment.verdict is Verdict.ACCEPTED
    assert assessment.abs_error == pytest.approx(0.01)


def test_assessor_rejects_error_beyond_tolerance_factor():
    assessment = assess_error(
        "x", result=2.0, reference=_ref(1.0), n=100, sum_abs=100.0,
        bound=0.1, tolerance_factor=4.0,
    )
    assert assessment.verdict is Verdict.REJECTED
    assert "exceeds" in assessment.reasons[0]


def test_assessor_is_inconclusive_when_reference_unreliable():
    # Oracle uncertainty (1e-3) dwarfs both the observed error and even the
    # claimed bound (4e-6): no honest adjudication is possible.
    assessment = assess_error(
        "x", result=1.0 + 1e-12, reference=_ref(1.0, uncertainty=1e-3),
        n=100, sum_abs=1.0, bound=1e-6,
    )
    assert assessment.verdict is Verdict.INCONCLUSIVE
    assert "straddles the adjudication" in assessment.reasons[0]


def test_assessor_reports_severe_cancellation_condition():
    # M = 1e18 makes the condition number ~2e17, beyond 1/u = 9.0e15: the
    # small terms are absent from the floating representation of M, so not
    # even Kahan can recover them on this input.
    values = synthetic.big_cancel(10, magnitude=1e18)
    info = kernels.assess(values)
    bound = naive_bound(values.size, float(np.sum(np.abs(values))))
    assessment = assess_error(
        "naive", kernels.naive_sum(values), reference_sum(values),
        values.size, float(np.sum(np.abs(values))), bound,
    )
    # The naive result is *within* its (huge, pessimistic) a-priori bound
    # even though it lost every small term: an important explainability case.
    assert assessment.verdict is Verdict.ACCEPTED
    assert assessment.condition_number > 1.0 / U
    assert assessment.abs_error == 10.0
    assert any("severe cancellation" in r for r in assessment.reasons)


def test_measured_error_explains_why_pairwise_partially_loses_ones():
    values = synthetic.big_cancel(100_000)
    ref = reference_sum(values).value
    pair_err = abs(kernels.pairwise_sum(values) - ref)
    naive_err = abs(kernels.naive_sum(values) - ref)
    kahan_err = abs(kernels.kahan_sum(values) - ref)
    assert pair_err == 26.0
    assert naive_err == 100_000.0
    assert kahan_err == 0.0
