"""Cross-validation against independently produced reference answers.

The reference answers here are NOT produced by the system under test:

* Test polynomials are built from roots fixed *a priori*, expanded to
  coefficients with NumPy, and only the expanded coefficient form is handed to
  the certifier (it never sees the factorisation).
* Root counts are independently cross-checked with ``numpy.roots`` and with a
  dense independent sign-change scan (tests/conftest.py).
* Every certified enclosure is checked to contain the pre-selected root and to
  satisfy the theorem hypotheses.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from tests.conftest import (
    assert_enclosure_contains,
    independent_sign_changes,
    verify_monotone_evidence,
    verify_newton_uniqueness_evidence,
)


def _expanded_polynomial_expression(roots: list[float]) -> tuple[str, list[float]]:
    """Expand prod(x - r_i) to coefficients and render an expression string.

    Expansion is done independently by NumPy; the certifier only receives the
    resulting 'c0 + c1*x + ...' string.
    """
    coeffs = np.array([1.0], dtype=float)
    for r in roots:
        coeffs = np.convolve(coeffs, [1.0, -r])
    # coeffs are highest-power first after convolve accumulation; convert to
    # ascending order for rendering.
    coeffs = coeffs[::-1]
    terms: list[str] = []
    for power, coef in enumerate(coeffs):
        if abs(coef) < 1e-12:
            continue
        if power == 0:
            terms.append(f"({coef:.15g})")
        elif power == 1:
            terms.append(f"({coef:.15g})*x")
        else:
            terms.append(f"({coef:.15g})*x^{power}")
    return " + ".join(terms), list(coeffs)


def _numpy_root_count(coefficients_desc: list[float]) -> int:
    """Independent real-root count via numpy.roots (descending coefficients)."""
    roots = np.roots(np.array(coefficients_desc[::-1], dtype=float))
    real = roots[np.abs(roots.imag) < 1e-8].real
    return len(real)


@pytest.mark.reference
def test_polynomial_with_three_priori_roots(run_payload) -> None:
    true_roots = [-1.75, 0.3, 2.4]
    expression, coeffs_asc = _expanded_polynomial_expression(true_roots)
    lo, hi = "-3", "3"

    payload = run_payload(expression, lo, hi)
    certified = payload["certified_roots"]
    assert len(certified) == 3

    # Every pre-selected root lands inside exactly one certified enclosure.
    for truth in true_roots:
        matches = [
            r
            for r in certified
            if Decimal(r["enclosure"]["lower"]) - Decimal("1e-12")
            <= Decimal(str(truth))
            <= Decimal(r["enclosure"]["upper"]) + Decimal("1e-12")
        ]
        assert len(matches) == 1, f"root {truth} not uniquely enclosed"
        assert_enclosure_contains(matches[0]["enclosure"], Decimal(str(truth)))

    # Independent NumPy agrees on the count.
    assert _numpy_root_count(coeffs_asc) == 3


@pytest.mark.reference
def test_single_priori_root_mid_interval(run_payload, oracle) -> None:
    expression, _ = _expanded_polynomial_expression([1.25])
    payload = run_payload(expression, "0", "2")
    certified = payload["certified_roots"]
    assert len(certified) == 1
    assert_enclosure_contains(certified[0]["enclosure"], Decimal("1.25"))


@pytest.mark.reference
def test_certified_count_matches_independent_sign_scan(run_payload) -> None:
    # A wiggly polynomial with five pre-selected simple roots.
    true_roots = [-2.0, -0.7, 0.1, 0.9, 1.8]
    expression, _ = _expanded_polynomial_expression(true_roots)
    lo, hi = -2.5, 2.5
    payload = run_payload(expression, str(lo), str(hi))
    certified = payload["certified_roots"]
    assert len(certified) == len(true_roots)

    # Independent dense scan (plain NumPy floats, not the SUT evaluator).
    # Reconstruct descending coefficients for the scan helper.
    coeffs = np.array([1.0])
    for r in true_roots:
        coeffs = np.convolve(coeffs, [1.0, -r])
    asc = coeffs[::-1]
    scan_count = independent_sign_changes(list(asc), lo, hi)
    # The boundary root -2.0 is interior to [-2.5,2.5]; all 5 change sign.
    assert scan_count == len(certified)


@pytest.mark.reference
def test_all_certified_evidence_independently_verifies(run_payload) -> None:
    expression, _ = _expanded_polynomial_expression([-1.2, 0.4, 1.6])
    payload = run_payload(expression, "-2", "2")
    for root in payload["certified_roots"]:
        kind = root["evidence"]["kind"]
        if kind == "interval_newton_uniqueness":
            verify_newton_uniqueness_evidence(root["evidence"])
        else:
            assert kind == "monotone_intermediate_value"
            verify_monotone_evidence(root["evidence"])


@pytest.mark.reference
def test_no_root_polynomial_cross_checked_with_numpy(run_payload) -> None:
    # (x-1)^2 + 1 = x^2 - 2x + 2: no real roots; NumPy reports a complex pair.
    payload = run_payload("x^2 - 2*x + 2", "-3", "3")
    assert payload["status"] == "root_free"
    roots = np.roots([1.0, -2.0, 2.0])
    assert np.all(np.abs(roots.imag) > 0.5)  # a conjugate pair, no real roots


@pytest.mark.reference
def test_approximate_layer_flags_unverified_and_agrees(run_payload) -> None:
    true_roots = [-0.5, 1.0]
    expression, _ = _expanded_polynomial_expression(true_roots)
    payload = run_payload(expression, "-2", "2", approximation=True)
    approx = payload["approximate_roots_unverified"]
    assert approx["status"] == "approximate_unverified"
    # The independent approximation layer finds the same two real roots.
    values = sorted(float(r["value"]) for r in approx["roots"])
    assert len(values) == 2
    assert abs(values[0] - (-0.5)) < 1e-8
    assert abs(values[1] - 1.0) < 1e-8
    # But they are explicitly NOT marked certified.
    for r in approx["roots"]:
        assert "certif" not in r["method"].lower()
