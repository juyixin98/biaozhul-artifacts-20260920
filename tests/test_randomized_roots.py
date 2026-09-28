"""Randomised property tests against independently built factorizations.

Polynomials are generated from explicit integer roots with chosen
multiplicities and a random rational content, so the true distinct real
roots and multiplicities are known WITHOUT consulting the kernel. Every run
must reproduce exactly that set, be pairwise disjoint, and agree with the
independent integer-oracle total count and the high-precision mpmath solver.
"""
from __future__ import annotations

import random
from fractions import Fraction as F

import pytest

from app import polynomial as P
from app.kernel import isolate_real_roots
from tests import oracles as O
from tests.test_kernel_isolation import TIGHT_SETTINGS


def _build(roots_with_mult, content=F(1)):
    """Build a polynomial from an independently specified root multiset."""
    poly = (F(1),)
    for root, mult in roots_with_mult:
        linear = (F(-root), F(1))
        for _ in range(mult):
            poly = P.mul(poly, linear)
    return P.scale(poly, content)


def _open_disjoint(cells):
    for a, b in zip(cells, cells[1:]):
        if a.exact and b.exact:
            if a.lo == b.lo:
                return False
        elif a.exact:
            if b.lo < a.lo < b.hi:
                return False
        elif b.exact:
            if a.lo < b.lo < a.hi:
                return False
        elif max(a.lo, b.lo) < min(a.hi, b.hi):
            return False
    return True


@pytest.mark.parametrize("seed", range(20))
def test_random_integer_root_polynomials(seed):
    rng = random.Random(seed)
    distinct = rng.sample(range(-6, 7), rng.randint(1, 6))
    roots_with_mult = [(r, rng.randint(1, 3)) for r in distinct]
    content = F(rng.randint(1, 5), rng.randint(1, 4))
    poly = _build(roots_with_mult, content)

    result = isolate_real_roots(poly, TIGHT_SETTINGS, target_width=F(1, 10**8))

    # 1. Number of distinct roots matches.
    truth = {F(r): m for r, m in roots_with_mult}
    assert result.distinct_real_roots == len(truth)

    # 2. Pairwise disjoint.
    assert _open_disjoint(result.cells)

    # 3. Every known root sits in exactly the cell carrying its multiplicity.
    for root_int, mult in roots_with_mult:
        root = F(root_int)
        holders = [
            c for c in result.cells
            if (c.exact and c.lo == root) or
            (not c.exact and c.lo < root < c.hi)
        ]
        assert len(holders) == 1, f"root {root} in {len(holders)} cells"
        assert holders[0].multiplicity == mult

    # 4. Independent integer-oracle distinct-root count agrees.
    #    Build an integer (denominator-cleared) coefficient tuple.
    denom = 1
    for c in poly:
        denom = denom * c.denominator
        # no need for true lcm; any common denominator keeps roots identical
    int_coeffs = tuple(int(c * denom) for c in poly)
    assert O.reference_total_real_roots(int_coeffs) == len(truth)

    # 5. Independent high-precision global solver agrees on the radical.
    radical = (F(1),)
    for r, _ in roots_with_mult:
        radical = P.mul(radical, (F(-r), F(1)))
    mp_roots = O.mpmath_real_roots(radical, dps=60)
    assert len(mp_roots) == len(truth)
    got = sorted(F(str(v)) for v in mp_roots)
    assert got == sorted(truth)


def test_rational_non_integer_root_isolated():
    # (3x - 2)(x + 5): roots 2/3 and -5.
    poly = P.mul((F(-2), F(3)), (F(5), F(1)))
    result = isolate_real_roots(poly, TIGHT_SETTINGS, target_width=F(1, 10**8))
    assert result.distinct_real_roots == 2
    located = {}
    for c in result.cells:
        if c.exact:
            located[c.lo] = c
        else:
            # Identify by bracketing.
            if c.lo < F(2, 3) < c.hi:
                located[F(2, 3)] = c
            elif c.lo < F(-5) < c.hi:
                located[F(-5)] = c
            elif c.lo < F(-5) == c.lo or c.hi == F(-5):
                located[F(-5)] = c
    # -5 may be hit exactly depending on the Cauchy grid; either form is fine.
    assert F(2, 3) in located
    assert any(
        (c.exact and c.lo == F(-5)) or (not c.exact and c.lo < F(-5) < c.hi)
        for c in result.cells
    )
