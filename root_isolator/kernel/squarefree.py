"""Square-free factorization and root bounding.

Repeated roots are invisible to a naive Sturm count on the original
polynomial: Sturm counts *distinct* roots and cannot assign multiplicities.
We therefore first decompose

    f = product_k F_k ** k

where every :math:`F_k` is monic and square-free and contains precisely the
factors that occur with multiplicity *k*. Running Sturm isolation on each
:math:`F_k` and tagging its roots with multiplicity *k* distinguishes repeated
from simple roots and separates different multiplicities exactly.
"""

from __future__ import annotations

from fractions import Fraction

from root_isolator.kernel.euclidean import exact_quotient, gcd_poly
from root_isolator.kernel.polynomial import RationalPoly


def square_free_factors(p: RationalPoly) -> list[tuple[int, RationalPoly]]:
    """Return ``[(multiplicity, factor), ...]`` with ``p = prod factor**m``.

    Factors are monic and square-free; constant factors are omitted. The
    algorithm only uses exact gcd/division over the rationals:

    ``R = gcd(f, f')`` carries each distinct factor with multiplicity
    ``m - 1``, so ``W = f / R`` carries every distinct factor once. Repeatedly
    peeling ``gcd(W, R)`` separates factors whose multiplicity equals the
    current index from higher-multiplicity ones.
    """

    if p.is_zero or p.degree == 0:
        return []

    monic = exact_quotient(p, RationalPoly([p.leading()]))
    radical = gcd_poly(monic, monic.derivative())  # gcd(f, f')
    if radical.is_zero:
        return [(1, monic)]
    work = exact_quotient(monic, radical)  # product of all distinct factors
    remainder = radical
    factors: list[tuple[int, RationalPoly]] = []
    multiplicity = 1
    while work.degree > 0:
        shared = gcd_poly(work, remainder)
        exact_multiplicity_part = exact_quotient(work, shared)
        if exact_multiplicity_part.degree > 0:
            factors.append((multiplicity, exact_multiplicity_part))
        work = shared
        if remainder.degree > 0:
            remainder = exact_quotient(remainder, shared)
        multiplicity += 1
    return factors


def cauchy_integer_bound(p: RationalPoly) -> Fraction:
    """Small integer ``B`` with every root ``z`` satisfying ``|z| < B``.

    Cauchy's bound gives ``|z| < 1 + max_{k<n} |a_k / a_n|``. We take its
    ceiling so ``B`` is an integer and bisection starts from exact dyadic
    endpoints. Computed entirely with :class:`Fraction`.
    """

    if p.degree <= 0:
        return Fraction(1)
    lead = p.leading()
    largest = max(
        (abs(p[k] / lead) for k in range(p.degree)),
        default=Fraction(0),
    )
    bound = largest + 1  # strict Cauchy bound
    ceiling = bound.numerator // bound.denominator
    if bound.numerator % bound.denominator:
        ceiling += 1
    return Fraction(max(ceiling, 1))
