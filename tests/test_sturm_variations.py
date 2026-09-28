"""Tests for Sturm sign-variation counts.

The central proof primitive is V(x), the number of sign changes in the Sturm
chain at x with zero rows skipped. These tests assert concrete sign sequences
and variation numbers against hand-computed values, and then compare the
application's chain against the test-only independent integer pseudo-remainder
oracle (:mod:`tests.oracles`) on a battery of polynomials.
"""
from __future__ import annotations

from fractions import Fraction as F

import pytest

from app import polynomial as P
from app.kernel import sign_variations
from tests import oracles as O


def test_sign_variations_skips_zeros_and_counts_flips():
    # Hand-computed sequences.
    assert sign_variations([1, -1, 1, -1]) == 3
    assert sign_variations([1, 1, 1, 1]) == 0
    assert sign_variations([-1, -1, 1, 1]) == 1
    # Zeros are skipped, never counted as a change (Sturm's convention).
    assert sign_variations([1, 0, -1, 0, 1]) == 2
    assert sign_variations([0, 0, 1]) == 0
    assert sign_variations([0, -1, 0]) == 0


def test_cubic_hand_computed_sign_table():
    # p = (x-1)(x-2)(x-3) = -6 + 11x - 6x^2 + x^3
    p = (F(-6), F(11), F(-6), F(1))
    chain = P.sturm_chain(p)

    def table(x: str) -> tuple[list[int], int]:
        signs = [P.sign_exact(q, F(x)) for q in chain]
        return signs, sign_variations(signs)

    # Hand-computed at the four critical locations.
    signs0, v0 = table("0")
    assert signs0 == [-1, 1, -1, 1] and v0 == 3
    signs1, v1 = table("1")   # root: leading zero row skipped
    assert signs1 == [0, 1, -1, 1] and v1 == 2
    signs2, v2 = table("2")
    assert signs2 == [0, -1, 0, 1] and v2 == 1
    signs3, v3 = table("3")
    assert signs3 == [0, 1, 1, 1] and v3 == 0
    signs4, v4 = table("4")
    assert signs4 == [1, 1, 1, 1] and v4 == 0

    # Explicit root-count statements from the hand table.
    assert v0 - v4 == 3          # three roots in (0, 4]
    assert v0 - v1 == 1          # one root in (0, 1] (namely 1)
    assert v1 - v2 == 1          # one root in (1, 2] (namely 2)
    assert v2 - v3 == 1          # one root in (2, 3] (namely 3)
    # Root exactly at the closed right endpoint IS counted.
    assert sign_variations([P.sign_exact(q, F(0)) for q in chain]) - \
        sign_variations([P.sign_exact(q, F(1)) for q in chain]) == 1
    # Root exactly at the open left endpoint is NOT counted.
    assert sign_variations([P.sign_exact(q, F(1)) for q in chain]) - \
        sign_variations([P.sign_exact(q, F(2)) for q in chain]) == 1


@pytest.mark.parametrize(
    "integer_coeffs,a,b",
    [
        ((-6, 11, -6, 1), "0", "4"),
        ((-6, 11, -6, 1), "1", "3"),
        ((-6, 11, -6, 1), "2", "2"),       # degenerate: same point
        ((1, 0, 1), "-10", "10"),           # no real roots
        ((1, 0, -1), "-5", "0"),            # one root (-1) in (-5, 0]
        ((-2, 5, -4, 1), "0", "4"),         # (x-1)^2(x-2): 2 distinct roots
        ((1,), "0", "1"),                   # constant
        ((0, 0, 1), "-100", "100"),         # x^2: one distinct root
    ],
)
def test_application_chain_matches_independent_integer_oracle(
        integer_coeffs, a, b):
    poly = P.trim(tuple(F(c) for c in integer_coeffs))
    if P.is_zero(poly) or P.degree(poly) == 0:
        return  # constants have no roots to count
    chain = P.sturm_chain(poly)
    app_count = sign_variations([P.sign_exact(q, F(a)) for q in chain]) - \
        sign_variations([P.sign_exact(q, F(b)) for q in chain])
    oracle_count = O.reference_sturm_count(integer_coeffs, F(a), F(b))
    assert app_count == oracle_count


@pytest.mark.parametrize(
    "integer_coeffs,expected_distinct",
    [
        ((-6, 11, -6, 1), 3),
        ((1, 0, 1), 0),
        ((-2, 5, -4, 1), 2),       # (x-1)^2 (x-2)
        ((54, -189, 261, -182, 68, -13, 1), 3),  # m=2,1,3
        ((1, 0, -1), 2),
        ((0, 1), 1),               # x
        ((1, 0, 0, 0, 1), 0),      # x^4 + 1
    ],
)
def test_total_real_root_count_matches_oracle(integer_coeffs, expected_distinct):
    poly = P.trim(tuple(F(c) for c in integer_coeffs))
    chain = P.sturm_chain(poly)
    signs_pos = [(1 if q[-1] > 0 else -1) for q in chain]
    signs_neg = [
        (1 if (q[-1] * ((-1) ** P.degree(q))) > 0 else -1) for q in chain
    ]
    app_total = sign_variations(signs_neg) - sign_variations(signs_pos)
    assert app_total == expected_distinct
    assert app_total == O.reference_total_real_roots(integer_coeffs)
