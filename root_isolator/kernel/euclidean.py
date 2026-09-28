"""Exact Euclidean arithmetic over :class:`RationalPoly`.

Everything here uses :class:`fractions.Fraction` coefficients, so results are
exact. Sturm remainders are rescaled by a *positive* constant only. A negative
rescaling would arbitrarily flip one chain member's signs and destroy the
"neighbours have opposite signs at an intermediate zero" property that Sturm's
theorem relies on; keeping factors positive is therefore a correctness rule,
not a style choice.
"""

from __future__ import annotations

from fractions import Fraction

from root_isolator.kernel.polynomial import RationalPoly, ZERO


def poly_divmod(a: RationalPoly, b: RationalPoly) -> tuple[RationalPoly, RationalPoly]:
    """Polynomial long division returning ``(quotient, remainder)`` with ``a = bq + r``."""

    if b.is_zero:
        raise ZeroDivisionError("polynomial division by zero")
    if a.is_zero or a.degree < b.degree:
        return ZERO, a

    remainder = list(a.coeffs)
    quotient = [Fraction(0)] * (a.degree - b.degree + 1)
    divisor = list(b.coeffs)
    lead = divisor[-1]
    while remainder and len(remainder) - 1 >= b.degree:
        shift = len(remainder) - 1 - b.degree
        factor = remainder[-1] / lead
        quotient[shift] = factor
        for j in range(len(divisor)):
            remainder[shift + j] -= factor * divisor[j]
        # The subtraction cancels the current top term; drop it.
        remainder.pop()
    return RationalPoly(quotient), RationalPoly(remainder)


def poly_remainder(a: RationalPoly, b: RationalPoly) -> RationalPoly:
    return poly_divmod(a, b)[1]


def exact_quotient(a: RationalPoly, b: RationalPoly) -> RationalPoly:
    """Divide when the remainder is known to be zero (factor divisibility)."""

    quotient, remainder = poly_divmod(a, b)
    if not remainder.is_zero:
        raise ValueError("exact_quotient called on a non-divisor")
    return quotient


def gcd_poly(a: RationalPoly, b: RationalPoly) -> RationalPoly:
    """Monic greatest common divisor over Q."""

    while not b.is_zero:
        a, b = b, poly_remainder(a, b)
    if a.is_zero:
        return a
    return exact_quotient(a, RationalPoly([a.leading()]))


def _positive_normalize(p: RationalPoly) -> RationalPoly:
    """Scale by a positive constant so the leading coefficient has magnitude 1."""

    lead = p.leading()
    factor = abs(lead)  # always > 0 for a non-zero polynomial
    return RationalPoly(c / factor for c in p.coeffs)


def sturm_chain(p: RationalPoly) -> list[RationalPoly]:
    """Build the canonical Sturm chain of ``p``.

    ``s_0 = p`` (normalised by a positive factor), ``s_1 = p'`` (normalised
    consistently), and ``s_{i+1}`` is the negated remainder of
    ``s_{i-1}`` divided by ``s_i``, with each member positively normalised to
    keep coefficient growth in check. The last member is a non-zero constant
    whenever ``p`` is square-free (the only case the isolator chains on).
    """

    if p.is_zero:
        return [ZERO]
    s0 = _positive_normalize(p)
    if p.degree == 0:
        return [s0]
    s1 = _positive_normalize(p.derivative())
    chain = [s0, s1]
    while not chain[-1].is_zero and chain[-1].degree > 0:
        remainder = poly_remainder(chain[-2], chain[-1])
        if remainder.is_zero:
            break
        chain.append(_positive_normalize(-remainder))
    return chain


def sign_sequence(chain: list[RationalPoly], x: Fraction) -> list[int]:
    """Signs of every chain member at ``x``, zeros included (``-1/0/+1``)."""

    return [member.eval_sign(x) for member in chain]


def variations_from_signs(signs: list[int]) -> int:
    """Count sign changes after deleting zeros.

    Zero entries are skipped rather than treated as either sign. At an
    intermediate member's zero the two neighbours always have opposite signs,
    so deletion leaves the variation count unchanged. At a zero of the FIRST
    member (``p(x) = 0``), skipping it evaluates the right-limit ``V(x+)``,
    which is exactly the convention the half-open ``(a, b]`` root counting uses.
    """

    nonzero = [s for s in signs if s != 0]
    return sum(1 for left, right in zip(nonzero, nonzero[1:]) if left != right)


def variations_at(chain: list[RationalPoly], x: Fraction) -> int:
    return variations_from_signs(sign_sequence(chain, x))
