"""Kernel tests: Sturm variation counts, isolation, multiplicities, endpoints.

Expected answers come from two independent sources, never from the kernel
itself:

* :mod:`tests.oracle` - a from-scratch sparse Sturm implementation;
* polynomials built *by construction* from known rational roots.
"""

from __future__ import annotations

from fractions import Fraction as F

import pytest

from root_isolator.kernel.euclidean import (
    sign_sequence,
    sturm_chain,
    variations_at,
)
from root_isolator.kernel.polynomial import RationalPoly
from root_isolator.kernel.isolate import isolate_roots
from root_isolator.kernel.squarefree import square_free_factors

from . import oracle


def _kernel_poly(sparse: oracle.SparsePoly) -> RationalPoly:
    return RationalPoly(oracle.to_dense_ascending(sparse))


def _radical_degree(poly: RationalPoly) -> int:
    factors = square_free_factors(poly)
    return sum(f.degree for _, f in factors)


# --------------------------------------------------------------------------- #
# Independent sign-variation cross-check (the headline constraint)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "roots",
    [
        [(F(-2), 1), (F(1), 2)],                 # (x+2)(x-1)^2
        [(F(0), 1), (F(1), 1)],                  # x(x-1)
        [(F(-1), 1), (F(1), 1)],                 # x^2-1
        [(F(1), 3)],                             # triple root
        [(F(1), 2), (F(3), 2)],                  # two double roots
        [(F(-3), 1), (F(2), 1), (F(7), 1)],      # three simple roots
    ],
)
def test_sturm_variation_counts_match_independent_oracle(roots, budget):
    sparse = oracle.poly_from_roots(roots)
    kernel = _kernel_poly(sparse)
    kernel_chain = sturm_chain(kernel)
    oracle_chain = oracle.sturm_chain_oracle(sparse)

    # Chains can be normalized differently but must have the same length.
    assert len(kernel_chain) == len(oracle_chain)

    for point in [F(-100), F(-2), F(-1), F(0), F(1), F(2), F(3), F(100)]:
        assert variations_at(kernel_chain, point) == oracle.variations_oracle(
            oracle_chain, point
        ), f"variation mismatch at x={point}"


def test_sign_sequence_includes_endpoint_zero():
    # (x-1): chain sign at x=1 must begin with an exact 0.
    poly = RationalPoly([F(-1), F(1)])
    chain = sturm_chain(poly)
    assert sign_sequence(chain, F(1))[0] == 0
    # Deleting that zero yields the right-limit V(1+) = 0 (no roots above 1).
    assert variations_at(chain, F(1)) == 0
    # Just below 1 there is one variation.
    assert variations_at(chain, F(0)) == 1


# --------------------------------------------------------------------------- #
# Repeated roots: multiplicity distinguished exactly
# --------------------------------------------------------------------------- #
def test_repeated_root_multiplicity_is_recovered(budget):
    sparse = oracle.poly_from_roots([(F(1), 2), (F(-2), 1)])
    result = isolate_roots(_kernel_poly(sparse), budget)
    assert result.kind == "isolated"
    assert result.distinct_real_roots == 2
    assert result.total_real_roots_with_multiplicity == 3

    by_region = {("neg" if iv.right <= 0 else "pos"): iv for iv in result.intervals}
    assert by_region["neg"].multiplicity == 1
    assert by_region["pos"].multiplicity == 2
    # Every reported interval proves exactly one distinct root.
    for iv in result.intervals:
        assert iv.proof.variations_left - iv.proof.variations_right == 1


def test_triple_root(budget):
    sparse = oracle.poly_from_roots([(F(2), 3)])
    result = isolate_roots(_kernel_poly(sparse), budget)
    assert result.distinct_real_roots == 1
    assert result.intervals[0].multiplicity == 3


def test_two_distinct_double_roots(budget):
    sparse = oracle.poly_from_roots([(F(1), 2), (F(3), 2)])
    result = isolate_roots(_kernel_poly(sparse), budget)
    assert result.distinct_real_roots == 2
    assert all(iv.multiplicity == 2 for iv in result.intervals)
    assert result.total_real_roots_with_multiplicity == 4


# --------------------------------------------------------------------------- #
# Close roots
# --------------------------------------------------------------------------- #
def test_extremely_close_roots_are_separated(budget):
    gap = F(1, 2 ** 50)
    sparse = oracle.poly_from_roots([(F(1), 1), (F(1) + gap, 1)])
    result = isolate_roots(_kernel_poly(sparse), budget)
    assert result.distinct_real_roots == 2
    intervals = sorted(result.intervals, key=lambda iv: iv.left)
    # The exact bisection lands on the dyadic shared endpoint 1.
    assert intervals[0].right == F(1)
    assert intervals[1].left == F(1)
    # Half-open convention: root at 1 belongs to the interval closing at 1.
    assert intervals[0].proof.right_endpoint_is_root is True
    assert intervals[1].proof.left_endpoint_is_root is True
    # Intervals remain disjoint (they only touch at the endpoint).
    assert intervals[0].right == intervals[1].left


# --------------------------------------------------------------------------- #
# No real roots
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "coeffs",
    [
        [F(1), F(0), F(1)],          # x^2 + 1
        [F(1), F(0), F(0), F(0), F(1)],  # x^4 + 1
        [F(2), F(0), F(3)],          # 3x^2 + 2
    ],
)
def test_polynomials_with_no_real_roots(coeffs, budget):
    result = isolate_roots(RationalPoly(coeffs), budget)
    assert result.kind == "isolated"
    assert result.distinct_real_roots == 0
    assert result.intervals == ()


# --------------------------------------------------------------------------- #
# High-dynamic-range coefficients stay exact
# --------------------------------------------------------------------------- #
def test_high_dynamic_range_tiny_roots_exact(budget):
    poly = RationalPoly([F(-1), F(0), F(10 ** 40)])
    result = isolate_roots(poly, budget)
    assert result.distinct_real_roots == 2
    for iv in result.intervals:
        # Exact endpoints, never float artifacts.
        assert isinstance(iv.left, F) and isinstance(iv.right, F)
    assert result.total_real_roots_with_multiplicity == 2


def test_high_dynamic_range_huge_roots_exact(budget):
    poly = RationalPoly([F(10 ** 40), F(0), F(-1)])
    result = isolate_roots(poly, budget)
    assert result.distinct_real_roots == 2


# --------------------------------------------------------------------------- #
# Special polynomials: zero and constants are not ordinary root lists
# --------------------------------------------------------------------------- #
def test_zero_polynomial_is_distinct_kind(budget):
    result = isolate_roots(RationalPoly([F(0)]), budget)
    assert result.kind == "zero_polynomial"
    assert result.intervals == ()
    assert "every real number" in result.notes[0]


def test_nonzero_constant_has_no_roots(budget):
    result = isolate_roots(RationalPoly([F(7)]), budget)
    assert result.kind == "constant_nonzero"
    assert result.intervals == ()


# --------------------------------------------------------------------------- #
# Known-root containment and disjointness, cross-checked by the oracle
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "roots",
    [
        [(F(-2), 1), (F(1), 2)],
        [(F(0), 1), (F(1), 1)],
        [(F(1), 2), (F(3), 2)],
        [(F(-5), 1), (F(0), 1), (F(5), 1)],
    ],
)
def test_each_known_root_lies_in_exactly_one_half_open_interval(roots, budget):
    sparse = oracle.poly_from_roots(roots)
    kernel = _kernel_poly(sparse)
    result = isolate_roots(kernel, budget)
    known = oracle.distinct_real_roots(roots)

    # Independent global Sturm count over the Cauchy span.
    oracle_chain = oracle.sturm_chain_oracle(sparse)
    bound = result.cauchy_bound
    total = oracle.count_distinct_roots_open_closed(
        oracle_chain, F(-bound), F(bound)
    )
    assert total == len(known)

    owners = 0
    for root in known:
        containing = [iv for iv in result.intervals if iv.left < root <= iv.right]
        assert len(containing) == 1, f"root {root} owned by {len(containing)} intervals"
        owners += 1
    assert owners == result.distinct_real_roots

    # Pairwise disjoint: interiors do not overlap.
    ordered = sorted(result.intervals, key=lambda iv: iv.left)
    for left, right in zip(ordered, ordered[1:]):
        assert left.right <= right.left


def test_squarefree_factorization_degree_matches(budget):
    sparse = oracle.poly_from_roots([(F(1), 2), (F(2), 1), (F(3), 3)])
    kernel = _kernel_poly(sparse)
    factors = square_free_factors(kernel)
    # (x-2) is simple -> multiplicity 1; (x-1) double -> 2; (x-3) triple -> 3.
    mults = {m: f.degree for m, f in factors}
    assert mults == {1: 1, 2: 1, 3: 1}
    assert _radical_degree(kernel) == 3
