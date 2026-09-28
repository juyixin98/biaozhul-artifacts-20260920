"""Symbolic phase: elimination tree and exact fill vs independent
dense boolean-elimination oracle."""
from __future__ import annotations

import numpy as np

from app.core import ordering as ordering_mod
from app.core.symbolic import symbolic_factor
from app.numerical_input.fixtures import (
    arrowhead, banded, grid_laplacian)
from app.numerical_input.sparse_matrix import from_coo


def _symbolic(fx, method):
    si = from_coo(fx.n, fx.rows, fx.cols, fx.vals)
    coo = si.upper.tocoo()
    order = ordering_mod.compute_ordering(
        method, coo.row, coo.col, si.n)
    return si, symbolic_factor(si.upper, order)


def test_tridiagonal_natural_has_zero_fill():
    si, sym = _symbolic(banded(12), "natural")
    assert sym.fill_entries == 0
    # every L column has at most 2 stored entries (pivot + one subdiag)
    assert int(sym.col_counts.max()) == 2
    assert sym.nnz_lower == sym.nnz_orig_lower + si.n


def test_fill_matches_dense_boolean_oracle_grid_natural(oracles):
    fx = grid_laplacian(5, 4)
    si, sym = _symbolic(fx, "natural")
    coo = si.upper.tocoo()
    expected = oracles.dense_bool_fill(
        coo.row, coo.col, fx.n, sym.ordering.perm)
    for k in range(fx.n):
        got = sym.column_rows(k)
        np.testing.assert_array_equal(
            got, expected[k],
            err_msg=f"L column {k} pattern mismatch")


def test_fill_matches_dense_boolean_oracle_minimum_degree(oracles):
    fx = grid_laplacian(6, 5)
    si, sym = _symbolic(fx, "minimum_degree")
    coo = si.upper.tocoo()
    expected = oracles.dense_bool_fill(
        coo.row, coo.col, fx.n, sym.ordering.perm)
    total = 0
    for k in range(fx.n):
        got = sym.column_rows(k)
        total += got.size
        np.testing.assert_array_equal(got, expected[k])
    assert total == sym.nnz_lower


def test_fill_matches_oracle_arrowhead_all_orderings(oracles):
    fx = arrowhead(15)
    for method in ("natural", "minimum_degree", "nested_dissection"):
        si, sym = _symbolic(fx, method)
        coo = si.upper.tocoo()
        expected = oracles.dense_bool_fill(
            coo.row, coo.col, fx.n, sym.ordering.perm)
        for k in range(fx.n):
            np.testing.assert_array_equal(
                sym.column_rows(k), expected[k])


def test_etree_roots_count_grid():
    # The shifted grid is connected, so the etree has exactly one root.
    _si, sym = _symbolic(grid_laplacian(4, 4), "natural")
    assert int(np.sum(sym.etree_parent == -1)) == 1


def test_etree_known_for_chain():
    # Column etree: parent[k] = first sub-diagonal row of L column k.
    # Natural-order chain 0-1-2-3: parent = [1, 2, 3, root(-1)].
    si, sym = _symbolic(banded(4), "natural")
    np.testing.assert_array_equal(
        sym.etree_parent, np.array([1, 2, 3, -1]))


def test_minimum_degree_reduces_grid_fill():
    _, nat = _symbolic(grid_laplacian(8, 7), "natural")
    _, md = _symbolic(grid_laplacian(8, 7), "minimum_degree")
    assert md.nnz_lower <= nat.nnz_lower
    # The grid must produce fill under natural ordering.
    assert nat.fill_entries > 0
    assert md.fill_entries < nat.fill_entries
