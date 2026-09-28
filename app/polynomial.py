"""Exact polynomial arithmetic over the rationals.

Polynomials are immutable tuples of :class:`fractions.Fraction` coefficients in
ascending power order (``coeffs[i]`` multiplies ``x**i``), with no trailing
zero coefficients. The zero polynomial is the empty tuple ``POLY_ZERO``.

Everything in this module is exact: no floats, no rounding. The precision-bound
numeric probes live in :mod:`app.kernel` and always escalate to the exact
routines here whenever a sign is not rigorously decided.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Iterable, Sequence

Coeff = Fraction
Poly = tuple[Fraction, ...]

POLY_ZERO: Poly = ()
POLY_ONE: Poly = (Fraction(1),)


def trim(coeffs: Sequence[Fraction]) -> Poly:
    """Strip trailing zero coefficients (defensive copy, immutable result)."""
    end = len(coeffs)
    while end > 0 and coeffs[end - 1] == 0:
        end -= 1
    return tuple(coeffs[:end])


def degree(p: Poly) -> int:
    """Degree of the polynomial; the zero polynomial has degree -1."""
    return len(p) - 1


def is_zero(p: Poly) -> bool:
    return len(p) == 0


def lc(p: Poly) -> Fraction:
    """Leading coefficient. Caller must ensure ``p`` is non-zero."""
    return p[-1]


def from_integers(values: Iterable[int]) -> Poly:
    return trim(tuple(Fraction(int(v)) for v in values))


def scale(p: Poly, k: Fraction) -> Poly:
    """Return ``k * p`` (exact); ``k == 0`` yields the zero polynomial."""
    if is_zero(p) or k == 0:
        return POLY_ZERO
    return trim(tuple(c * k for c in p))


def add(a: Poly, b: Poly) -> Poly:
    n = max(len(a), len(b))
    out = [Fraction(0) for _ in range(n)]
    for i, c in enumerate(a):
        out[i] += c
    for i, c in enumerate(b):
        out[i] += c
    return trim(tuple(out))


def sub(a: Poly, b: Poly) -> Poly:
    n = max(len(a), len(b))
    out = [Fraction(0) for _ in range(n)]
    for i, c in enumerate(a):
        out[i] += c
    for i, c in enumerate(b):
        out[i] -= c
    return trim(tuple(out))


def mul(a: Poly, b: Poly) -> Poly:
    if is_zero(a) or is_zero(b):
        return POLY_ZERO
    out = [Fraction(0) for _ in range(len(a) + len(b) - 1)]
    for i, ci in enumerate(a):
        for j, cj in enumerate(b):
            out[i + j] += ci * cj
    return trim(tuple(out))


def derivative(p: Poly) -> Poly:
    if len(p) <= 1:
        return POLY_ZERO
    return trim(tuple(i * p[i] for i in range(1, len(p))))


def divmod_poly(a: Poly, b: Poly) -> tuple[Poly, Poly]:
    """Exact polynomial long division over Q.

    Returns quotient and remainder with ``a == q*b + r`` and
    ``deg(r) < deg(b)``. Raises :class:`ZeroDivisionError` for a zero divisor.
    """
    if is_zero(b):
        raise ZeroDivisionError("polynomial division by zero")
    if is_zero(a) or len(a) < len(b):
        return POLY_ZERO, a
    remainder = list(a)
    quotient = [Fraction(0) for _ in range(len(a) - len(b) + 1)]
    shift = len(b) - 1
    for k in range(len(quotient) - 1, -1, -1):
        qk = remainder[k + shift] / b[-1]
        if qk != 0:
            quotient[k] = qk
            for j in range(len(b)):
                remainder[k + j] -= qk * b[j]
    return trim(tuple(quotient)), trim(tuple(remainder[:shift]))


def remainder(a: Poly, b: Poly) -> Poly:
    return divmod_poly(a, b)[1]


def monic(p: Poly) -> Poly:
    """Normalise to leading coefficient 1 (over Q this is exact)."""
    if is_zero(p):
        return POLY_ZERO
    top = lc(p)
    if top == 1:
        return p
    return tuple(c / top for c in p)  # already trimmed, lc becomes 1


def gcd(a: Poly, b: Poly) -> Poly:
    """Monic greatest common divisor over Q[x] via the Euclidean algorithm."""
    if is_zero(a):
        return monic(b)
    if is_zero(b):
        return monic(a)
    r0, r1 = a, b
    while not is_zero(r1):
        r0, r1 = r1, remainder(r0, r1)
    return monic(r0)


def sturm_chain(p: Poly) -> list[Poly]:
    """Classical Sturm chain of a *square-free* polynomial.

    ``P0 = p``, ``P1 = p'``, ``P_{k+1} = -rem(P_{k-1}, P_k)`` terminating at a
    non-zero constant. For a square-free input (``gcd(p, p') == 1``) the chain
    has ``deg(p) + 1`` members. Returned members share no scalar factor
    semantics callers depend on — only their signs matter.
    """
    if is_zero(p):
        raise ValueError("Sturm chain is undefined for the zero polynomial")
    p0 = p
    p1 = derivative(p)
    if is_zero(p1):
        # Non-zero constant: single-member chain, V is identically 0.
        return [p0]
    chain = [p0, p1]
    while True:
        nxt = scale(remainder(chain[-2], chain[-1]), Fraction(-1))
        if is_zero(nxt):
            # Normal termination: the previous member divided the one before
            # with zero remainder. For a square-free input that member is a
            # non-zero constant; the zero remainder is not itself appended.
            return chain
        chain.append(nxt)
        if degree(nxt) == 0:
            return chain


def eval_exact(p: Poly, x: Fraction) -> Fraction:
    """Exact Horner evaluation at a rational point."""
    value = Fraction(0)
    for c in reversed(p):
        value = value * x + c
    return value


def sign_exact(p: Poly, x: Fraction) -> int:
    """Exact sign at a rational point: -1, 0 or +1."""
    value = eval_exact(p, x)
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0
