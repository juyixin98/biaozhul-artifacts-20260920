"""Unit tests for exact rational polynomial arithmetic."""
from __future__ import annotations

from fractions import Fraction as F

import pytest

from app import polynomial as P


def test_trim_strips_trailing_zeros_and_is_immutable():
    source = [F(1), F(0), F(2), F(0)]
    result = P.trim(source)
    assert result == (F(1), F(0), F(2))
    # Original list must not be mutated (immutability rule).
    assert source == [F(1), F(0), F(2), F(0)]


def test_add_sub_mul_division_identities():
    a = (F(1), F(2))          # 1 + 2x
    b = (F(-1), F(3))         # -1 + 3x
    assert P.add(a, b) == (F(0), F(5))
    assert P.sub(a, b) == (F(2), F(-1))
    product = P.mul(a, b)     # (1+2x)(-1+3x) = -1 + x + 6x^2
    assert product == (F(-1), F(1), F(6))
    q, r = P.divmod_poly(product, b)
    assert r == P.POLY_ZERO
    assert q == a


def test_derivative_and_degree():
    assert P.derivative((F(5), F(4), F(3), F(2))) == (F(4), F(6), F(6))
    assert P.degree(P.POLY_ZERO) == -1
    assert P.is_zero(P.trim((F(0),)))


def test_gcd_separates_repeated_factor():
    x1 = (F(-1), F(1))
    p = P.mul(P.mul(x1, x1), (F(-2), F(1)))  # (x-1)^2 (x-2)
    g = P.gcd(p, P.derivative(p))
    assert g == (F(-1), F(1))  # monic x-1


@pytest.mark.parametrize(
    "coeffs,point,expected",
    [
        ((F(-6), F(11), F(-6), F(1)), F(2), 0),     # root
        ((F(-6), F(11), F(-6), F(1)), F(0), -1),    # -6
        ((F(-6), F(11), F(-6), F(1)), F(4), 1),     # +6
        ((F(1), F(0), F(1)), F(7), 1),              # x^2+1 always positive
    ],
)
def test_exact_sign_at_known_points(coeffs, point, expected):
    assert P.sign_exact(coeffs, point) == expected


def test_division_by_zero_polynomial_raises():
    with pytest.raises(ZeroDivisionError):
        P.divmod_poly((F(1), F(1)), P.POLY_ZERO)


def test_monic_normalisation_is_exact():
    p = (F(3), F(6), F(9))  # lc 9
    m = P.monic(p)
    assert m[-1] == 1
    assert m == (F(1, 3), F(2, 3), F(1))
