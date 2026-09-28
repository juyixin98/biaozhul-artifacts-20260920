"""Concrete assertions on the isolation kernel.

Each test states exact expected results (multiplicities, exact rational roots,
interval containment, pairwise disjointness) rather than merely that the API
is callable. Locations are cross-checked against the independent mpmath
global solver.
"""
from __future__ import annotations

from fractions import Fraction as F

import pytest

from app import polynomial as P
from app.errors import BudgetExceeded, FailureCode
from app.kernel import isolate_real_roots
from app.settings import KernelSettings
from tests import oracles as O


TIGHT_SETTINGS = KernelSettings(
    precision_dps=100,
    max_degree=200,
    max_coefficients=201,
    max_bisections=20000,
    max_sturm_length=202,
    max_coeff_bits=20000,
)


def _cells_as_fractions(result):
    return [(c.lo, c.hi, c.multiplicity, c.exact) for c in result.cells]


def _assert_disjoint(cells):
    # Cells are sorted; consecutive located root sets must not intersect.
    # Exact singletons are closed points; open cells exclude their endpoints.
    for a, b in zip(cells, cells[1:]):
        if a.exact and b.exact:
            assert a.lo != b.lo
        elif a.exact:
            assert not (b.lo < a.lo < b.hi)
        elif b.exact:
            assert not (a.lo < b.lo < a.hi)
        else:
            assert a.hi <= b.lo or b.hi <= a.lo


def _assert_contains_mpmath(cells, oracle_roots, tol):
    """Each mpmath root must lie in exactly one matching cell."""
    matched = 0
    for value in oracle_roots:
        hit = None
        for c in cells:
            vf = F(str(value))
            if c.exact:
                if vf == c.lo:
                    hit = c
            else:
                if c.lo < vf < c.hi:
                    hit = c
        assert hit is not None, f"oracle root {value} lies in no cell"
        matched += 1
    assert matched == len(oracle_roots)
    assert len(cells) == len(oracle_roots)


def test_repeated_roots_report_distinct_roots_with_multiplicity():
    # (x-1)^2 (x-2) (x-3)^3
    x1, x2, x3 = (F(-1), F(1)), (F(-2), F(1)), (F(-3), F(1))
    poly = P.mul(P.mul(P.mul(P.mul(x1, x1), x2), x3), P.mul(x3, x3))
    result = isolate_real_roots(poly, TIGHT_SETTINGS, target_width=F(1, 10**6))

    assert result.degree == 6
    assert result.distinct_real_roots == 3
    assert result.real_roots_with_multiplicity == 2 + 1 + 3

    by_location = {round(float(c.lo), 3): c for c in result.cells}
    c1, c2, c3 = by_location[1.0], by_location[2.0], by_location[3.0]
    assert c1.multiplicity == 2 and not c1.exact
    assert c2.multiplicity == 1 and c2.exact and c2.lo == F(2)
    assert c3.multiplicity == 3 and not c3.exact
    for c in (c1, c3):
        assert c.width <= F(1, 10**6)
    _assert_disjoint(result.cells)

    # Independent oracle on the RADICAL (x-1)(x-2)(x-3), constructed directly
    # from the known factorization rather than from the kernel output. Global
    # iterative solvers struggle with repeated roots; the radical has only
    # simple roots and its real roots are exactly the distinct real roots.
    radical = P.mul(P.mul(x1, x2), x3)  # (x-1)(x-2)(x-3)
    oracle = O.mpmath_real_roots(radical)
    _assert_contains_mpmath(result.cells, oracle, F(1, 10**50))


def test_extremely_close_roots_are_separated():
    # x^2 - 10^-12: roots at +/-10^-6.
    poly = P.trim((F(-1, 10**12), F(0), F(1)))
    result = isolate_real_roots(poly, TIGHT_SETTINGS,
                                target_width=F(1, 10**14))
    assert result.distinct_real_roots == 2
    negative, positive = result.cells
    assert negative.hi < 0 < positive.lo  # genuinely separated around zero
    assert negative.width <= F(1, 10**14)
    assert positive.width <= F(1, 10**14)
    _assert_disjoint(result.cells)

    oracle = O.mpmath_real_roots(poly)
    assert len(oracle) == 2
    _assert_contains_mpmath(result.cells, oracle, F(1, 10**40))


def test_no_real_roots_returns_empty_with_matching_variations():
    # (x^2+1)(x^4+1) = x^6 + x^4 + x^2 + 1
    poly = P.trim((F(1), F(0), F(1), F(0), F(1), F(0), F(1)))
    result = isolate_real_roots(poly, TIGHT_SETTINGS, target_width=F(1, 1000))
    assert result.distinct_real_roots == 0
    assert result.cells == []
    # Each square-free factor's report records zero isolated roots.
    assert all(fr.isolated_roots == 0 for fr in result.factor_reports)
    assert O.reference_total_real_roots(
        (1, 0, 1, 0, 1, 0, 1)) == 0


def test_high_dynamic_range_coefficients():
    # (10^20 x - 1)(x + 10^-20) = 10^20 x^2 - 10^-20, roots +/-10^-20.
    poly = P.trim((F(-1, 10**20), F(0), F(10**20)))
    result = isolate_real_roots(poly, TIGHT_SETTINGS,
                                target_width=F(1, 10**22))
    assert result.distinct_real_roots == 2
    negative, positive = result.cells
    assert negative.hi < 0 < positive.lo
    for c in (negative, positive):
        assert abs(c.lo) < F(1, 10**19) and abs(c.hi) < F(1, 10**19)
    _assert_disjoint(result.cells)
    oracle = O.mpmath_real_roots(poly)
    _assert_contains_mpmath(result.cells, oracle, F(1, 10**40))


def test_roots_exactly_at_user_interval_endpoints():
    x1, x2, x3 = (F(-1), F(1)), (F(-2), F(1)), (F(-3), F(1))
    poly = P.mul(P.mul(P.mul(P.mul(x1, x1), x2), x3), P.mul(x3, x3))
    # Search interval (1, 3]: both endpoints are roots, of mult 2 and 3.
    result = isolate_real_roots(
        poly, TIGHT_SETTINGS, target_width=F(1, 10**6),
        search_interval=(F(1), F(3)),
    )
    exact_points = {c.lo: c for c in result.cells if c.exact}
    assert set(exact_points) == {F(1), F(2), F(3)}
    assert exact_points[F(1)].multiplicity == 2
    assert exact_points[F(2)].multiplicity == 1
    assert exact_points[F(3)].multiplicity == 3
    assert result.distinct_real_roots == 3


def test_closed_interval_reports_each_endpoint_root_exactly_once():
    # Closed search interval [1, 3]: roots sit at BOTH endpoints (1 has
    # multiplicity 2, 3 has multiplicity 3). Each must be reported exactly
    # once — no double counting from the half-open internal convention.
    x1, x2, x3 = (F(-1), F(1)), (F(-2), F(1)), (F(-3), F(1))
    poly = P.mul(P.mul(P.mul(P.mul(x1, x1), x2), x3), P.mul(x3, x3))
    result = isolate_real_roots(
        poly, TIGHT_SETTINGS, target_width=F(1, 10**6),
        search_interval=(F(1), F(3)),
    )
    counts: dict[F, int] = {}
    for c in result.cells:
        counts[c.lo] = counts.get(c.lo, 0) + 1
    assert counts.get(F(1)) == 1 and counts.get(F(3)) == 1
    assert result.distinct_real_roots == 3


def test_interval_between_roots_isolates_only_interior_root():
    # Strictly inside: (3/2, 5/2) brackets only the root at 2.
    x1, x2, x3 = (F(-1), F(1)), (F(-2), F(1)), (F(-3), F(1))
    poly = P.mul(P.mul(P.mul(P.mul(x1, x1), x2), x3), P.mul(x3, x3))
    result = isolate_real_roots(
        poly, TIGHT_SETTINGS, target_width=F(1, 10**6),
        search_interval=(F(3, 2), F(5, 2)),
    )
    assert result.distinct_real_roots == 1
    assert result.cells[0].exact and result.cells[0].lo == F(2)


def test_rational_root_hit_during_bisection_is_exact_singleton():
    # For x - 2 the exact Cauchy bracket is [-4, 4], whose first midpoint is
    # exactly the rational root 2: it must be emitted as an exact singleton
    # rather than an open enclosure.
    poly = (F(-2), F(1))
    result = isolate_real_roots(poly, TIGHT_SETTINGS, target_width=F(1, 10**9))
    assert result.distinct_real_roots == 1
    cell = result.cells[0]
    assert cell.exact and cell.lo == F(2) and cell.hi == F(2)


def test_nonzero_constant_has_no_roots():
    result = isolate_real_roots((F(7),), TIGHT_SETTINGS, target_width=F(1, 100))
    assert result.degree == 0
    assert result.cells == []


def test_bisection_budget_exceeded_is_explicit():
    settings = KernelSettings(
        precision_dps=80, max_degree=200, max_coefficients=201,
        max_bisections=5,  # impossibly tight
        max_sturm_length=202, max_coeff_bits=20000,
    )
    with pytest.raises(BudgetExceeded) as excinfo:
        isolate_real_roots((F(-6), F(11), F(-6), F(1)), settings,
                           target_width=F(1, 10**30))
    assert excinfo.value.code == FailureCode.BISECTION_BUDGET_EXCEEDED
    assert "bisections_used" in excinfo.value.state
