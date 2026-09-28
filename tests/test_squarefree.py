"""Tests for exact square-free factorisation (multiplicity separation)."""
from __future__ import annotations

from fractions import Fraction as F

from app import polynomial as P
from app.squarefree import square_free_decomposition


def _factors_by_multiplicity(dec):
    return {sf.multiplicity: sf.factor for sf in dec.factors}


def _reconstruct(dec, poly):
    running = (F(1),)
    for sf in dec.factors:
        for _ in range(sf.multiplicity):
            running = P.mul(running, sf.factor)
    return P.scale(running, dec.content) == poly


def test_constant_has_no_factors():
    dec = square_free_decomposition((F(5),))
    assert dec.content == 5 and dec.factors == ()


def test_single_simple_polynomial_is_one_factor_of_multiplicity_one():
    # (x-1)(x-2)
    poly = P.mul((F(-1), F(1)), (F(-2), F(1)))
    dec = square_free_decomposition(poly)
    by_m = _factors_by_multiplicity(dec)
    assert set(by_m) == {1}
    assert by_m[1] == P.monic(poly)
    assert _reconstruct(dec, poly)


def test_repeated_roots_split_into_distinct_multiplicity_factors():
    # (x-1)^2 (x-2) (x-3)^3
    x1, x2, x3 = (F(-1), F(1)), (F(-2), F(1)), (F(-3), F(1))
    poly = P.mul(P.mul(P.mul(P.mul(x1, x1), x2), x3), P.mul(x3, x3))
    dec = square_free_decomposition(poly)
    by_m = _factors_by_multiplicity(dec)
    assert set(by_m) == {1, 2, 3}
    assert by_m[1] == (F(-2), F(1))       # x - 2
    assert by_m[2] == (F(-1), F(1))       # x - 1
    assert by_m[3] == (F(-3), F(1))       # x - 3
    assert dec.content == 1
    assert _reconstruct(dec, poly)


def test_high_multiplicity_single_root():
    # (x + 4)^5
    base = (F(4), F(1))
    poly = base
    for _ in range(4):
        poly = P.mul(poly, base)
    dec = square_free_decomposition(poly)
    by_m = _factors_by_multiplicity(dec)
    assert set(by_m) == {5}
    assert by_m[5] == base
    assert _reconstruct(dec, poly)


def test_rational_coefficients_with_leading_content():
    # (1/2)(x-1)(x-2): content carries the rational scalar.
    base = P.mul((F(-1), F(1)), (F(-2), F(1)))
    poly = P.scale(base, F(1, 2))
    dec = square_free_decomposition(poly)
    by_m = _factors_by_multiplicity(dec)
    assert by_m[1] == P.monic(base)  # factors monic regardless of content
    assert dec.content == F(1, 2)
    assert _reconstruct(dec, poly)


def test_factors_are_pairwise_coprime():
    x1, x2, x3 = (F(-1), F(1)), (F(-2), F(1)), (F(-3), F(1))
    poly = P.mul(P.mul(P.mul(P.mul(x1, x1), x2), x3), P.mul(x3, x3))
    dec = square_free_decomposition(poly)
    factors = [sf.factor for sf in dec.factors]
    for i in range(len(factors)):
        for j in range(i + 1, len(factors)):
            assert P.gcd(factors[i], factors[j]) == P.POLY_ONE
