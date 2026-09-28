"""Exact square-free factorisation over Q[x] (Musser's algorithm).

Every non-zero polynomial decomposes uniquely as

    f = c * s_1^1 * s_2^2 * ... * s_k^k

where each ``s_i`` is a monic square-free polynomial, pairwise coprime, and
every real (or complex) root of ``s_i`` is a root of ``f`` of multiplicity
exactly ``i``. This is what lets the service *distinguish repeated roots*: a
root of ``s_2`` is reported with multiplicity 2, without needing to know the
root algebraically.

All operations are exact rational polynomial operations; no factoring of
integer coefficients is required, so the routine is robust against
high-dynamic-range coefficients.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import polynomial as P
from .polynomial import Fraction, Poly


@dataclass(frozen=True)
class SquareFreeFactor:
    """A square-free factor carrying roots of one fixed multiplicity."""

    multiplicity: int
    factor: Poly  # monic, square-free, pairwise coprime with siblings


@dataclass(frozen=True)
class SquareFreeDecomposition:
    content: Fraction
    """Rational scalar multiplier (sign/leading content of ``f``)."""

    factors: tuple[SquareFreeFactor, ...]
    """Factors ordered by ascending multiplicity; only non-constant ones."""

    def factor_for_multiplicity(self, multiplicity: int) -> Poly | None:
        for f in self.factors:
            if f.multiplicity == multiplicity:
                return f.factor
        return None


def _exact_quotient(a: Poly, b: Poly) -> Poly:
    """Division that asserts the divisor divides the dividend exactly."""
    q, r = P.divmod_poly(a, b)
    if not P.is_zero(r):
        raise AssertionError(
            "internal error: exact factorisation produced a non-divisor"
        )
    return q


def square_free_decomposition(p: Poly) -> SquareFreeDecomposition:
    """Return the exact square-free decomposition of a non-zero polynomial.

    Constants decompose to a content-only result with no factors.
    """
    if P.is_zero(p):
        raise ValueError("the zero polynomial has no square-free decomposition")
    if P.degree(p) == 0:
        return SquareFreeDecomposition(content=P.lc(p), factors=())

    content = P.lc(p)
    monic_p = P.monic(p)
    derivative = P.derivative(monic_p)

    g = P.gcd(monic_p, derivative)   # ∏ s_k^(k-1)
    h = _exact_quotient(monic_p, g)  # ∏ s_k

    factors: list[SquareFreeFactor] = []
    multiplicity = 1
    while not P.is_zero(h) and h != P.POLY_ONE:
        t = P.gcd(h, g)
        s_i = _exact_quotient(h, t)
        if s_i != P.POLY_ONE:
            factors.append(SquareFreeFactor(multiplicity, s_i))
        h = t
        if not P.is_zero(g):
            g = _exact_quotient(g, t)
        multiplicity += 1

    return SquareFreeDecomposition(content=content, factors=tuple(factors))
