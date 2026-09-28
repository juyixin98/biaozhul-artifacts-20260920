"""Unit tests for the exact RationalPoly type and Euclidean helpers."""

from __future__ import annotations

from fractions import Fraction as F

import pytest

from root_isolator.kernel.euclidean import (
    gcd_poly,
    poly_divmod,
    poly_remainder,
    sturm_chain,
)
from root_isolator.kernel.polynomial import ONE, ZERO, RationalPoly


def test_construction_trims_trailing_zeros_and_coerces_ints():
    poly = RationalPoly([1, 2, 0, 0])
    assert poly.degree == 1
    assert all(isinstance(c, F) for c in poly.coeffs)
    assert ZERO.is_zero and ZERO.degree == -1
    assert ONE.leading() == 1


def test_internal_division_stays_exact():
    # (x+1)/2 constructed from ints must hold Fractions, never floats.
    poly = RationalPoly([1, 1]) * F(1, 2)
    assert poly.coeffs == (F(1, 2), F(1, 2))
    assert all(isinstance(c, F) for c in poly.coeffs)


def test_addition_subtraction_and_negation():
    a = RationalPoly([1, 0, 1])
    b = RationalPoly([0, 1])
    assert (a + b).coeffs == (F(1), F(1), F(1))
    assert (a - b).coeffs == (F(1), F(-1), F(1))
    assert (-a).coeffs == (F(-1), F(0), F(-1))


def test_multiplication_known_expansion():
    # (x-1)(x+2) = x^2 + x - 2
    product = RationalPoly([-1, 1]) * RationalPoly([2, 1])
    assert product.coeffs == (F(-2), F(1), F(1))


def test_power_by_squaring():
    base = RationalPoly([-1, 1])  # x - 1
    assert (base ** 0) == ONE
    assert (base ** 1) == base
    assert (base ** 2).coeffs == (F(1), F(-2), F(1))
    assert (base ** 3).coeffs == (F(-1), F(3), F(-3), F(1))


def test_derivative():
    assert RationalPoly([3, 2, 1]).derivative().coeffs == (F(2), F(2))
    assert RationalPoly([5]).derivative().is_zero


def test_exact_horner_evaluation_and_sign():
    poly = RationalPoly([-2, 0, 1])  # x^2 - 2
    assert poly.eval(F(0)) == -2
    assert poly.eval_sign(F(2)) == 1
    assert poly.eval_sign(F(1)) == -1
    assert poly.eval_sign(F(0)) == -1


def test_repr_readable():
    assert "x^2" in repr(RationalPoly([0, 0, 1]))
    assert repr(ZERO) == "RationalPoly(0)"


def test_scale_to_integer_clears_denominators():
    poly = RationalPoly([F(1, 2), F(3, 4)])
    integer_poly = poly.scale_to_integer()
    assert all(c.denominator == 1 for c in integer_poly.coeffs)
    assert integer_poly.coeffs == (F(2), F(3))


def test_divmod_exact_quotient_and_remainder():
    # (x^2 - 1) / (x + 1) = x - 1, remainder 0
    quotient, remainder = poly_divmod(RationalPoly([-1, 0, 1]), RationalPoly([1, 1]))
    assert quotient.coeffs == (F(-1), F(1))
    assert remainder.is_zero


def test_divmod_with_nonzero_remainder():
    # (x^2 + 1) / (x + 1) = x - 1 remainder 2
    quotient, remainder = poly_divmod(RationalPoly([1, 0, 1]), RationalPoly([1, 1]))
    assert quotient.coeffs == (F(-1), F(1))
    assert remainder.coeffs == (F(2),)
    # Recomposition: a = b*q + r
    a = RationalPoly([1, 0, 1])
    b = RationalPoly([1, 1])
    assert a == b * quotient + remainder


def test_division_by_zero_polynomial_raises():
    with pytest.raises(ZeroDivisionError):
        poly_divmod(RationalPoly([1, 1]), ZERO)


def test_gcd_detects_repeated_factor():
    # (x-1)^2 and (x-1) share gcd x-1
    double = RationalPoly([-1, 1]) ** 2
    single = RationalPoly([-1, 1])
    assert gcd_poly(double, single) == single
    # Coprime polynomials share only a constant.
    assert gcd_poly(RationalPoly([1, 0, 1]), RationalPoly([1, 1])).degree == 0


def test_remainder_helper_matches_divmod():
    a = RationalPoly([1, 2, 3])
    b = RationalPoly([1, 1])
    assert poly_remainder(a, b) == poly_divmod(a, b)[1]


def test_sturm_chain_constant_and_zero():
    assert len(sturm_chain(RationalPoly([7]))) == 1
    assert sturm_chain(ZERO) == [ZERO]


def test_equality_and_indexing_outside_range():
    poly = RationalPoly([1, 2])
    assert poly != RationalPoly([1, 3])
    assert poly[5] == 0
    assert poly[0] == 1
