"""Exact rational univariate polynomial type.

Coefficients are :class:`fractions.Fraction`, so every arithmetic operation
used by the Sturm kernel is exact. Polynomials are stored ascending by power
(``coeffs[k]`` is the coefficient of ``x**k``) with no trailing zero.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Iterable, Iterator, Sequence

Coeff = Fraction


class RationalPoly:
    """Immutable polynomial over the rationals."""

    __slots__ = ("coeffs",)

    def __init__(self, coeffs: Iterable[Fraction | int]) -> None:
        # Coerce every coefficient to Fraction centrally. Relying on callers to
        # pass Fractions would let an internal int/int use Python's true
        # division and silently introduce binary floats into "exact" arithmetic.
        trimmed = [Fraction(c) for c in coeffs]
        while len(trimmed) > 0 and trimmed[-1] == 0:
            trimmed.pop()
        if not trimmed:
            trimmed = []
        object.__setattr__(self, "coeffs", tuple(trimmed))

    # ----- basic properties -------------------------------------------------
    @property
    def degree(self) -> int:
        return len(self.coeffs) - 1

    @property
    def is_zero(self) -> bool:
        return not self.coeffs

    def leading(self) -> Fraction:
        return self.coeffs[-1]

    def __getitem__(self, power: int) -> Fraction:
        if 0 <= power < len(self.coeffs):
            return self.coeffs[power]
        return Fraction(0)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RationalPoly) and self.coeffs == other.coeffs

    def __hash__(self) -> int:
        return hash(self.coeffs)

    def __repr__(self) -> str:
        if self.is_zero:
            return "RationalPoly(0)"
        terms = []
        for k in range(self.degree, -1, -1):
            c = self.coeffs[k]
            if c == 0:
                continue
            if k == 0:
                terms.append(f"{c}")
            elif k == 1:
                terms.append(f"{c}*x")
            else:
                terms.append(f"{c}*x^{k}")
        return "RationalPoly(" + " + ".join(terms).replace("+ -", "- ") + ")"

    # ----- arithmetic -------------------------------------------------------
    def __add__(self, other: "RationalPoly") -> "RationalPoly":
        n = max(len(self.coeffs), len(other.coeffs))
        return RationalPoly(
            (self.coeffs[k] if k < len(self.coeffs) else 0)
            + (other.coeffs[k] if k < len(other.coeffs) else 0)
            for k in range(n)
        )

    def __sub__(self, other: "RationalPoly") -> "RationalPoly":
        n = max(len(self.coeffs), len(other.coeffs))
        return RationalPoly(
            (self.coeffs[k] if k < len(self.coeffs) else 0)
            - (other.coeffs[k] if k < len(other.coeffs) else 0)
            for k in range(n)
        )

    def __neg__(self) -> "RationalPoly":
        return RationalPoly(-c for c in self.coeffs)

    def __mul__(self, other: "RationalPoly | Fraction | int") -> "RationalPoly":
        if isinstance(other, (Fraction, int)):
            return RationalPoly(c * other for c in self.coeffs)
        if not isinstance(other, RationalPoly):
            return NotImplemented
        if self.is_zero or other.is_zero:
            return ZERO
        out = [Fraction(0)] * (self.degree + other.degree + 1)
        for i, ci in enumerate(self.coeffs):
            if ci == 0:
                continue
            for j, cj in enumerate(other.coeffs):
                out[i + j] += ci * cj
        return RationalPoly(out)

    __rmul__ = __mul__

    def __pow__(self, exponent: int) -> "RationalPoly":
        if not isinstance(exponent, int) or exponent < 0:
            return NotImplemented
        result = ONE
        base = self
        while exponent:
            if exponent & 1:
                result = result * base
            base = base * base
            exponent >>= 1
        return result

    def scale_to_integer(self) -> "RationalPoly":
        """Return an integer-coefficient polynomial defining the same roots."""

        if self.is_zero:
            return self
        from math import lcm

        den = 1
        for c in self.coeffs:
            den = lcm(den, c.denominator)
        return self * den

    # ----- calculus ---------------------------------------------------------
    def derivative(self) -> "RationalPoly":
        if self.degree <= 0:
            return ZERO
        return RationalPoly(k * self.coeffs[k] for k in range(1, len(self.coeffs)))

    def eval(self, x: Fraction) -> Fraction:
        """Exact Horner evaluation."""

        result = Fraction(0)
        for c in reversed(self.coeffs):
            result = result * x + c
        return result

    def eval_sign(self, x: Fraction) -> int:
        value = self.eval(x)
        return (value > 0) - (value < 0)

    # ----- factories --------------------------------------------------------
    @staticmethod
    def from_ascending(coeffs: Sequence[Fraction]) -> "RationalPoly":
        return RationalPoly(iter(coeffs))

    def iter_nonzero(self) -> Iterator[tuple[int, Fraction]]:
        for k, c in enumerate(self.coeffs):
            if c != 0:
                yield k, c


ZERO = RationalPoly([])
ONE = RationalPoly([Fraction(1)])
