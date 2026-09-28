"""Tests for elimination tree and symbolic fill against dense references."""
from __future__ import annotations

import numpy as np
from scipy import sparse

from sparse_cholesky.core.etree import NO_PARENT, elimination_tree, postorder
from sparse_cholesky.core.ordering import (
    compute_ordering,
    permute_matrix,
)
from sparse_cholesky.core.symbolic import symbolic_factorization


def _sparse_pattern_matrix(symbolic, n):
    return sparse.csc_matrix(
        (np.ones(symbolic.row_idx.size, dtype=np.int8),
         symbolic.row_idx, symbolic.col_ptr),
        shape=(n, n),
    ).toarray().astype(bool)


def test_etree_edges_point_to_larger_index_or_root(spd_cases):
    for matrix in spd_cases:
        parent = elimination_tree(matrix.csc)
        assert parent.shape == (matrix.n,)
        for i, p in enumerate(parent):
            assert p == NO_PARENT or p > i


def test_etree_tridiagonal_is_single_chain():
    # The textbook case: tridiagonal 5 -> chain 0-1-2-3-4 rooted at 4.
    n = 5
    rows, cols = [], []
    for i in range(n):
        rows.append(i); cols.append(i)
        if i > 0:
            rows.append(i); cols.append(i - 1)
    from sparse_cholesky.input.matrix import build_sparse_matrix
    m = build_sparse_matrix(n, rows, cols, [2.0] * len(rows))
    parent = elimination_tree(m.csc)
    assert parent.tolist() == [1, 2, 3, 4, NO_PARENT]


def test_postorder_is_valid_permutation(spd_cases):
    for matrix in spd_cases:
        parent = elimination_tree(matrix.csc)
        order = postorder(parent)
        assert sorted(order.tolist()) == list(range(matrix.n))
        # Every child appears before its parent.
        pos = {int(v): i for i, v in enumerate(order)}
        for child, p in enumerate(parent):
            if p != NO_PARENT:
                assert pos[child] < pos[int(p)]


def test_symbolic_pattern_matches_dense_boolean_reference(spd_cases, ref):
    for matrix in spd_cases:
        for ordering in ("natural", "rcm"):
            perm = compute_ordering(ordering, matrix.csc)
            aperm = permute_matrix(matrix.csc, perm)
            symbolic = symbolic_factorization(aperm)
            sparse_pattern = _sparse_pattern_matrix(symbolic, matrix.n)
            reference = ref.bool_pattern(aperm.toarray())
            assert np.array_equal(sparse_pattern, reference), (
                f"pattern mismatch for n={matrix.n} ordering={ordering}"
            )


def test_symbolic_fill_counts_are_consistent(spd_cases):
    for matrix in spd_cases:
        symbolic = symbolic_factorization(matrix.csc)
        assert symbolic.fill_in == symbolic.nnz_lower - symbolic.nnz_a_lower
        assert symbolic.fill_in >= 0
        # Each column pattern is sorted and starts on its diagonal.
        for k, rows in enumerate(symbolic.col_rows):
            assert rows[0] == k
            assert np.all(np.diff(rows) > 0)


def test_rcm_reduces_or_preserves_grid_bandwidth_and_fill():
    from sparse_cholesky.input.fixtures import grid2d_laplacian
    from sparse_cholesky.input.matrix import build_sparse_matrix

    fx = grid2d_laplacian(6)
    matrix = build_sparse_matrix(*fx.keys())
    natural = symbolic_factorization(matrix.csc)
    perm = compute_ordering("rcm", matrix.csc)
    rcm = symbolic_factorization(permute_matrix(matrix.csc, perm))
    # RCM must not increase fill for this structured grid.
    assert rcm.nnz_lower <= natural.nnz_lower


def test_dense_block_fill_confined_to_block():
    from sparse_cholesky.input.fixtures import spd_with_dense_block
    from sparse_cholesky.input.matrix import build_sparse_matrix

    block, tail = 5, 4
    matrix = build_sparse_matrix(*spd_with_dense_block(block, tail).keys())
    symbolic = symbolic_factorization(matrix.csc)
    n = matrix.n
    # No predicted L entry may cross from the dense block into the tail.
    for col, rows in enumerate(symbolic.col_rows):
        for r in rows:
            if col >= block:  # tail columns: only the diagonal
                assert r == col
            else:
                assert r < block or r == col
