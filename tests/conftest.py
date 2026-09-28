"""Independent reference oracle shared by the tests.

CRITICAL: nothing in this module imports the certification kernel under test
(``app.core.certifier``) or its interval evaluator. Ground-truth numbers are
computed independently with the standard-library ``decimal`` module:

* square roots  - Decimal Newton iteration
* pi            - Chudnovsky series
* ln(2)         - 2*atanh(1/3) series
* e             - Taylor series of exp(1)

Constructed polynomials use roots fixed *a priori* as decimal constants, so the
expected answer is known before the system under test runs.
"""

from __future__ import annotations

from decimal import Decimal, getcontext

import pytest

getcontext().prec = 100

ZERO = Decimal(0)
ONE = Decimal(1)
TWO = Decimal(2)
THREE = Decimal(3)


# ---------------------------------------------------------------------------
# Independently computed mathematical constants
# ---------------------------------------------------------------------------
def decimal_sqrt(value: Decimal | str | int, iters: int = 120) -> Decimal:
    """Newton square root in exact Decimal arithmetic (no use of the SUT)."""
    x = Decimal(value)
    if x < 0:
        raise ValueError("sqrt of negative")
    if x == 0:
        return ZERO
    guess = x / TWO
    for _ in range(iters):
        guess = (guess + x / guess) / TWO
    return guess


def decimal_pi(terms: int = 10) -> Decimal:
    """Chudnovsky: pi = 426880 sqrt(10005) / sum M_k L_k / X_k.

    Uses the direct closed-form integer coefficient M_k = (6k)! / ((3k)!
    (k!)^3) rather than a remembered recurrence. About 10 terms exceed 100
    decimal digits.
    """
    from math import factorial

    getcontext().prec = 130
    c = Decimal(426880) * decimal_sqrt(10005)
    x_unit = Decimal(-262537412640768000)  # -640320^3
    x_dec = Decimal(1)
    series = Decimal(13591409)  # k = 0 term
    for k in range(1, terms):
        m_int = factorial(6 * k) // (factorial(3 * k) * factorial(k) ** 3)
        l_int = 13591409 + 545140134 * k
        x_dec *= x_unit
        series += Decimal(m_int) * Decimal(l_int) / x_dec
    return c / series


def decimal_ln2(terms: int = 200) -> Decimal:
    """ln(2) = 2 * atanh(1/3), an independent fast series."""
    third = ONE / THREE
    total = ZERO
    power = third
    for k in range(terms):
        total += power / (2 * k + 1)
        power *= third * third
    return TWO * total


def decimal_e(terms: int = 80) -> Decimal:
    total = ONE
    factorial = ONE
    for k in range(1, terms):
        factorial *= k
        total += ONE / factorial
    return total


# ---------------------------------------------------------------------------
# Independent theorem-evidence re-verification (pure Decimal)
# ---------------------------------------------------------------------------
def verify_newton_uniqueness_evidence(evidence: dict) -> None:
    """Re-check the interval-Newton uniqueness hypotheses from evidence.

    The theorem requires X ∩ N(X) ⊂ int(X). We independently re-evaluate the
    endpoint comparisons from the recorded high-precision decimal strings.
    """
    search = evidence["search_interval"]
    inter = evidence["image_intersection"]
    assert Decimal(inter[0]) > Decimal(search[0]), (
        "intersection must be strictly inside X on the left"
    )
    assert Decimal(inter[1]) < Decimal(search[1]), (
        "intersection must be strictly inside X on the right"
    )
    assert Decimal(inter[0]) <= Decimal(inter[1])


def verify_monotone_evidence(evidence: dict) -> None:
    """Independently re-check the monotone-IVT hypotheses."""
    d_lo = Decimal(evidence["derivative_lower_bound"])
    d_hi = Decimal(evidence["derivative_upper_bound"])
    f_left_lo, f_left_hi = map(Decimal, evidence["f_at_left_enclosure"])
    f_right_lo, f_right_hi = map(Decimal, evidence["f_at_right_enclosure"])
    if evidence["monotonicity"] == "increasing":
        assert d_lo > 0
        assert f_left_hi <= 0 <= f_right_lo
    else:
        assert d_hi < 0
        assert f_left_lo >= 0 >= f_right_hi


def assert_enclosure_contains(enclosure: dict, true_value: Decimal) -> None:
    lo = Decimal(enclosure["lower"])
    hi = Decimal(enclosure["upper"])
    assert lo <= true_value <= hi, (
        f"certified enclosure [{lo}, {hi}] misses true root {true_value}"
    )


# ---------------------------------------------------------------------------
# Independent polynomial evaluation (numpy floats, never the SUT evaluator)
# ---------------------------------------------------------------------------
def poly_value(coefficients: list[float], x: float) -> float:
    """Evaluate polynomial given ascending-power coefficients via Horner."""
    result = 0.0
    for coef in reversed(coefficients):
        result = result * x + coef
    return result


def independent_sign_changes(
    coefficients: list[float], lo: float, hi: float, samples: int = 200_001
) -> int:
    """Count distinct sign changes on a fine independent grid."""
    import numpy as np

    grid = np.linspace(lo, hi, samples)
    values = np.array([poly_value(coefficients, float(x)) for x in grid])
    changes = int(np.sum(values[:-1] * values[1:] < 0))
    endpoints = int(values[0] == 0.0) + int(values[-1] == 0.0)
    return changes + endpoints


@pytest.fixture(scope="session")
def oracle() -> "Oracle":
    return Oracle()


class Oracle:
    """Bundle of independent constants for convenient test access."""

    SQRT2 = decimal_sqrt(2)
    PI = decimal_pi()
    LN2 = decimal_ln2()
    E = decimal_e()


# ---------------------------------------------------------------------------
# Service-level test fixtures (these DO exercise the SUT, by design)
# ---------------------------------------------------------------------------
@pytest.fixture
def run_payload():
    """Run the full service and return the JSON-safe payload dict."""
    from app.core.config import CertConfig
    from app.core.tracer import Tracer
    from app.services import certify_service

    def _run(expression, lo, hi, *, approximation=False, **config_kw):
        config = CertConfig(**config_kw) if config_kw else CertConfig()
        with Tracer.create(None) as tracer:
            return certify_service.certify_expression(
                expression=expression,
                lower=str(lo),
                upper=str(hi),
                config=config,
                tracer=tracer,
                include_approximation=approximation,
            )

    return _run


@pytest.fixture
def run_kernel():
    """Run the certifier directly, returning the internal result object."""
    from app.core.certifier import Certifier
    from app.core.config import CertConfig
    from app.core.derivative import differentiate
    from app.core.evaluator import Evaluator
    from app.core.parser import parse_expression
    from app.core.tracer import Tracer

    def _run(expression, lo, hi, dps=50, **config_kw):
        ast = parse_expression(expression)
        ev = Evaluator(dps)
        config = CertConfig(**config_kw) if config_kw else CertConfig()
        with Tracer.create(None) as tracer:
            certifier = Certifier(
                ast, differentiate(ast), ev, config, tracer
            )
            result = certifier.run(ev.mp.mpf(str(lo)), ev.mp.mpf(str(hi)))
        return result, ev

    return _run
