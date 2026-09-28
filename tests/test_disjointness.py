"""Tests for the disjointness post-pass across multiplicity factors.

Roots belonging to different square-free factors are isolated on independent
dyadic grids, so their cells can initially overlap. The post-pass must refine
them until pairwise disjoint while keeping the right multiplicity on each.
"""
from __future__ import annotations

from fractions import Fraction as F

from app import polynomial as P
from app.kernel import isolate_real_roots
from tests.test_kernel_isolation import TIGHT_SETTINGS


def _open_cells_intersect(a, b):
    if a.exact and b.exact:
        return a.lo == b.lo
    if a.exact:
        return b.lo < a.lo < b.hi
    if b.exact:
        return a.lo < b.lo < a.hi
    return max(a.lo, b.lo) < min(a.hi, b.hi)


def test_cross_factor_nearby_roots_are_separated_with_multiplicity():
    # (x-1)^2 * (x - 1000001/1000000): two distinct roots only 10^-6 apart,
    # of different multiplicity. A deliberately coarse target width forces
    # the per-factor cells to overlap initially.
    root_a = (F(-1), F(1))
    root_b = (F(-1000001, 1000000), F(1))
    poly = P.mul(P.mul(root_a, root_a), root_b)

    result = isolate_real_roots(poly, TIGHT_SETTINGS, target_width=F(1, 10))

    assert result.distinct_real_roots == 2
    # No pair of returned cells intersects.
    for i in range(len(result.cells)):
        for j in range(i + 1, len(result.cells)):
            assert not _open_cells_intersect(result.cells[i], result.cells[j])

    # Each cell contains the correct root with the correct multiplicity.
    cells = result.cells
    mult2 = [c for c in cells if c.multiplicity == 2]
    mult1 = [c for c in cells if c.multiplicity == 1]
    assert len(mult2) == 1 and len(mult1) == 1
    c2, c1 = mult2[0], mult1[0]
    assert c2.lo < F(1) < c2.hi or c2.exact and c2.lo == F(1)
    other = F(1000001, 1000000)
    assert c1.lo < other < c1.hi or c1.exact and c1.lo == other


def test_evenly_spaced_integer_roots_all_disjoint():
    # (x)(x-1)(x-2)(x-3)(x-4): five simple roots; coarse width forces overlap.
    factors = [(F(-k), F(1)) for k in range(5)]
    poly = factors[0]
    for f in factors[1:]:
        poly = P.mul(poly, f)
    result = isolate_real_roots(poly, TIGHT_SETTINGS, target_width=F(5))
    assert result.distinct_real_roots == 5
    for i in range(len(result.cells)):
        for j in range(i + 1, len(result.cells)):
            assert not _open_cells_intersect(result.cells[i], result.cells[j])
