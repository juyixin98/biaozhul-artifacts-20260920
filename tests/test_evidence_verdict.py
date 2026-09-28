"""Evidence-layer tests: exact Descartes witness and verdict tri-state.

The exact Descartes/Vincent witness is cross-validated against the independent
sparse Sturm oracle, and verdict acceptance is asserted on concrete cases
(repeated roots, close roots, no real roots, high dynamic range).
"""

from __future__ import annotations

from fractions import Fraction as F

import pytest

from root_isolator.evidence.descartes import (
    count_roots_half_open,
    descartes_check,
)
from root_isolator.evidence.verifier import build_verdict
from root_isolator.kernel.isolate import isolate_roots
from root_isolator.kernel.polynomial import RationalPoly

from . import oracle


def _kernel(sparse: oracle.SparsePoly) -> RationalPoly:
    return RationalPoly(oracle.to_dense_ascending(sparse))


def _radical(roots: list[F]) -> RationalPoly:
    return RationalPoly(
        oracle.to_dense_ascending(oracle.poly_from_roots([(r, 1) for r in roots]))
    )


# --------------------------------------------------------------------------- #
# Exact Descartes/Vincent witness independently cross-checked
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "roots,a,b",
    [
        ([F(0)], F(-2), F(2)),          # x^3+x style: single root 0, wide interval
        ([F(-1), F(1)], F(-2), F(2)),   # x^2 - 1 wide
        ([F(1), F(1) + F(1, 2 ** 50)], F(1), F(2)),  # right excludes 1 -> only 1 root
        ([F(1), F(1) + F(1, 2 ** 50)], F(0), F(1)),  # includes root exactly at b=1
        ([F(2)], F(2), F(5)),           # root at excluded left endpoint -> 0
        ([F(2)], F(0), F(2)),           # root at included right endpoint -> 1
        ([F(-3), F(0), F(2)], F(-3), F(2)),
    ],
)
def test_descartes_counter_matches_independent_sturm(roots, a, b):
    radical = _radical(roots)
    rad_sparse = oracle.poly_from_roots([(r, 1) for r in roots])
    ochain = oracle.sturm_chain_oracle(rad_sparse)

    count, conclusive = count_roots_half_open(radical, a, b)
    expected = oracle.count_distinct_roots_open_closed(ochain, a, b)
    assert conclusive is True
    assert count == expected


def test_descartes_wide_interval_x_cubed_plus_x_is_one_not_three():
    # Regression for the naive single-transform Descartes (returned 3).
    poly = RationalPoly([F(0), F(1), F(0), F(1)])  # x^3 + x, root 0
    count, conclusive = count_roots_half_open(poly, F(-2), F(2))
    assert conclusive is True
    assert count == 1


def test_descartes_check_agrees_on_isolating_interval_with_endpoint_root(budget):
    radical = _radical([F(1), F(1) + F(1, 2 ** 50)])
    # Kernel's left interval closes exactly at 1: (0, 1].
    evidence = descartes_check(radical, F(0), F(1), expect_root_at_right=True)
    assert evidence.agrees is True
    assert evidence.root_at_right_endpoint is True
    assert evidence.descartes_root_count == 1


def test_descartes_check_flags_wrong_endpoint_claim(budget):
    radical = _radical([F(1)])
    evidence = descartes_check(radical, F(0), F(1), expect_root_at_right=False)
    # Root really is at b=1 but caller claimed not -> disagreement.
    assert evidence.agrees is False
    assert "endpoint disagreement" in evidence.reason


# --------------------------------------------------------------------------- #
# Verdict tri-state on concrete polynomials
# --------------------------------------------------------------------------- #
def _verdict(coeffs, budget, numeric):
    poly = RationalPoly([F(c) if not isinstance(c, F) else c for c in coeffs])
    return build_verdict(poly, isolate_roots(poly, budget), numeric)


def test_verdict_accepted_for_repeated_root(budget, numeric):
    verdict = _verdict([2, -3, 0, 1], budget, numeric)  # (x-1)^2 (x+2)
    assert verdict.status == "accepted"
    assert verdict.distinct_real_roots == 2
    assert verdict.total_real_roots_with_multiplicity == 3
    assert verdict.complex_roots_with_multiplicity == 0
    multiplicities = sorted(iv.multiplicity for iv in verdict.intervals)
    assert multiplicities == [1, 2]


def test_verdict_accepted_for_no_real_roots(budget, numeric):
    verdict = _verdict([1, 0, 1], budget, numeric)
    assert verdict.status == "accepted"
    assert verdict.distinct_real_roots == 0
    assert verdict.complex_roots_with_multiplicity == 2
    assert verdict.intervals == ()


def test_verdict_special_zero_and_constant(budget, numeric):
    zero = build_verdict(
        RationalPoly([F(0)]), isolate_roots(RationalPoly([F(0)]), budget), numeric
    )
    assert zero.status == "accepted" and zero.result_kind == "zero_polynomial"

    const = build_verdict(
        RationalPoly([F(5)]), isolate_roots(RationalPoly([F(5)]), budget), numeric
    )
    assert const.status == "accepted" and const.result_kind == "constant_nonzero"


def test_verdict_accepted_simple_distinct_roots(budget, numeric):
    sparse = oracle.poly_from_roots([(F(-2), 1), (F(0), 1), (F(3), 1)])
    poly = _kernel(sparse)
    verdict = build_verdict(poly, isolate_roots(poly, budget), numeric)
    assert verdict.status == "accepted"
    assert verdict.distinct_real_roots == 3
    # Every interval carries all three evidence kinds.
    for bundle in verdict.intervals:
        assert bundle.exact_descartes_agrees is True
        assert bundle.numeric_confirmed is True
        assert bundle.sturm["variations_left"] - bundle.sturm["variations_right"] == 1


def test_verdict_indeterminate_for_sub_float64_close_roots(budget, numeric):
    gap = F(1, 2 ** 50)
    sparse = oracle.poly_from_roots([(F(1), 1), (F(1) + gap, 1)])
    poly = _kernel(sparse)
    verdict = build_verdict(poly, isolate_roots(poly, budget), numeric)
    # Exact answer correct and high-precision mpmath confirms it ...
    assert verdict.distinct_real_roots == 2
    assert verdict.mpmath.status == "confirmed"
    # ... but float64 cannot separate them -> indeterminate, never rejected.
    assert verdict.float64.status == "inconclusive"
    assert verdict.status == "indeterminate"


def test_degree_accounting_separates_real_and_complex(budget, numeric):
    # (x-1)^2 (x^2+1) = degree 4: 2 real (mult) + 2 complex
    poly = RationalPoly([F(-1), F(1)]) ** 2 * RationalPoly([F(1), F(0), F(1)])
    verdict = build_verdict(poly, isolate_roots(poly, budget), numeric)
    assert verdict.total_real_roots_with_multiplicity == 2
    assert verdict.complex_roots_with_multiplicity == 2
