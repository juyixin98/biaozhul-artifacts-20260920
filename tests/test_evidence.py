"""Evidence bundle: residual, reconstruction, LAPACK & mpmath agreement,
plus fill/ordering comparison reporting."""
from __future__ import annotations

import numpy as np

from app.numerical_input.fixtures import (
    arrowhead, banded, grid_laplacian)
from app.numerical_input.sparse_matrix import from_coo
from app.evidence.metrics import mpmath_reference_solve, full_symmetric


def test_evidence_passes_on_grid(engine):
    fx = grid_laplacian(6, 5)
    r = engine.solve(fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
                     ordering="minimum_degree", with_mpmath=True)
    assert r.evidence.passed, r.evidence.reasons
    assert r.evidence.residual_rel < 1e-10
    assert r.evidence.dense_solution_error < 1e-9
    assert r.evidence.mpmath_solution_error < 1e-6


def test_mpmath_matches_lapack_small():
    fx = banded(8)
    si = from_coo(fx.n, fx.rows, fx.cols, fx.vals)
    x_mp = mpmath_reference_solve(si.upper, fx.rhs, dps=40)
    x_np = np.linalg.solve(full_symmetric(si.upper).toarray(), fx.rhs)
    assert np.max(np.abs(x_mp - x_np)) < 1e-10


def test_fill_ratio_reported_and_bounded(engine):
    fx = grid_laplacian(10, 9)
    r = engine.solve(fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
                     ordering="natural", with_mpmath=False)
    assert r.evidence.fill_ratio >= 1.0
    assert r.report.fill_entries > 0


def test_ordering_compare_reports_fill_reduction(engine):
    fx = grid_laplacian(10, 9)
    rows = engine.compare_orderings(
        fx.n, fx.rows, fx.cols, fx.vals)
    by = {r["method"]: r for r in rows}
    assert by["minimum_degree"]["nnz_l"] <= by["natural"]["nnz_l"]
    assert by["nested_dissection"]["nnz_l"] <= by["natural"]["nnz_l"]
    # every row reports concrete integers, not placeholders
    for r in rows:
        assert r["nnz_l"] > 0
        assert r["fill_ratio"] >= 1.0


def test_arrowhead_evidence_with_mpmath(engine):
    fx = arrowhead(18)
    r = engine.solve(fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
                     ordering="nested_dissection")
    assert r.evidence.passed, r.evidence.reasons
