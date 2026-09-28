"""Tests for independent error-evidence assembly."""
from __future__ import annotations

from fractions import Fraction as F

from app import polynomial as P
from app.evidence import (
    companion_real_roots,
    verify_isolation,
)
from app.kernel import isolate_real_roots
from tests.test_kernel_isolation import TIGHT_SETTINGS


def _run(poly, width=F(1, 10**6)):
    result = isolate_real_roots(poly, TIGHT_SETTINGS, target_width=width)
    factors = {sf.multiplicity: sf.factor
               for sf in result.decomposition.factors}
    radical = P.POLY_ONE
    for sf in result.decomposition.factors:
        radical = P.mul(radical, sf.factor)
    report = verify_isolation(
        poly, radical, factors, result.cells, width, 80, full_line=True,
    )
    return result, report


def test_simple_cubic_all_cells_accepted():
    poly = P.trim(tuple(F(c) for c in (-6, 11, -6, 1)))
    result, report = _run(poly, F(1, 10**6))
    assert report.accepted is True
    assert len(report.cells) == 3
    for cell in report.cells:
        assert cell.verdict == "accepted"
        assert cell.width_ok is True
        # Open cells must have opposite-sign strict endpoint enclosures.
        assert cell.enclosure_lo.sign != 0
        assert cell.enclosure_hi.sign != 0
        assert cell.enclosure_lo.sign != cell.enclosure_hi.sign
        assert cell.brentq_root is not None


def test_companion_witness_matches_count_when_reliable():
    poly = P.trim(tuple(F(c) for c in (-6, 11, -6, 1)))
    _, report = _run(poly)
    assert report.cross_check.reliable is True
    assert report.cross_check.distinct_real_roots == 3


def test_companion_witness_marked_unreliable_for_high_dynamic_range():
    poly = P.trim((F(-1, 10**20), F(0), F(10**20)))
    _, report = _run(poly, F(1, 10**22))
    assert report.cross_check.reliable is False
    # Exact answer is still accepted via brentq + interval enclosures.
    assert report.accepted is True


def test_no_real_roots_has_empty_cells_and_zero_cross_count():
    poly = P.trim(tuple(F(c) for c in (1, 0, 1, 0, 1, 0, 1)))
    _, report = _run(poly)
    assert report.cells == ()
    assert report.cross_check.distinct_real_roots == 0
    assert report.accepted is True


def test_exact_singleton_enclosure_contains_zero():
    # x - 2: Cauchy midpoint is exactly the root.
    poly = (F(-2), F(1))
    result, report = _run(poly)
    exact = [c for c in report.cells if True]
    assert len(exact) == 1
    assert exact[0].enclosure_lo.contains_zero is True
    assert exact[0].verdict == "accepted"


def test_evidence_rejects_a_forged_oversized_cell():
    # Construct a genuine isolation, then tamper one cell to be too wide;
    # the independent evidence must flag it rather than trusting the kernel.
    from app.kernel import RootCell
    poly = P.trim(tuple(F(c) for c in (-6, 11, -6, 1)))
    result, _ = _run(poly)
    forged = [
        RootCell(
            lo=c.lo, hi=c.hi + 100, multiplicity=c.multiplicity, exact=False,
            variations_left=c.variations_left,
            variations_right=c.variations_right,
            chain_length=c.chain_length,
        ) for c in result.cells
    ]
    factors = {sf.multiplicity: sf.factor
               for sf in result.decomposition.factors}
    radical = P.POLY_ONE
    for sf in result.decomposition.factors:
        radical = P.mul(radical, sf.factor)
    report = verify_isolation(
        poly, radical, factors, forged, F(1, 10**6), 80, full_line=True,
    )
    assert report.accepted is False
    assert any(c.verdict == "rejected" for c in report.cells)


def test_companion_real_roots_helper():
    locations, reliable = companion_real_roots(
        P.trim(tuple(F(c) for c in (-6, 11, -6, 1)))
    )
    assert reliable is True
    assert len(locations) == 3
