"""Shared pytest fixtures and independent oracles.

IMPORTANT: the oracles here are implemented independently of the
``app.core`` kernels (dense boolean elimination, dense LAPACK solve,
explicit LDL formulae). They are the grading reference, never derived
from the code under test.
"""
from __future__ import annotations

import numpy as np
import pytest
import scipy.sparse as sp

from app.numerical_input import fixtures
from app.numerical_input.sparse_matrix import from_coo
from app.service.engine import FactorizationEngine


@pytest.fixture
def engine():
    # Fresh cache per test so reuse assertions are deterministic.
    return FactorizationEngine(enable_cache=True)


@pytest.fixture
def all_spd_fixtures():
    return [
        fixtures.grid_laplacian(),
        fixtures.banded(),
        fixtures.arrowhead(),
    ]


def dense_bool_fill(upper_rows, upper_cols, n, perm):
    """Independent oracle: filled pattern via naive boolean elimination.

    Works on the permuted matrix. Returns list[n] of sorted arrays of
    row indices i>=k that are nonzero in column k of L. O(n^3) but the
    test matrices are small — it is a reference, not a product kernel.
    """
    inv = np.empty(n, dtype=np.int64)
    inv[perm] = np.arange(n)
    f = np.zeros((n, n), dtype=bool)
    np.fill_diagonal(f, True)
    for r, c in zip(upper_rows.tolist(), upper_cols.tolist()):
        a, b = inv[r], inv[c]
        f[a, b] = True
        f[b, a] = True

    for k in range(n):
        below = [i for i in range(k, n) if f[i, k]]
        for a_i in range(len(below)):
            for b_i in range(a_i, len(below)):
                i, j = below[a_i], below[b_i]
                f[i, j] = True
                f[j, i] = True
    return [np.sort(np.where(f[k:, k])[0] + k) for k in range(n)]


def dense_oracle_solve(rows, cols, vals, n, b):
    """Independent LAPACK reference built straight from COO."""
    a = np.zeros((n, n), dtype=np.float64)
    for r, c, v in zip(rows, cols, vals):
        a[r, c] = v
    return np.linalg.solve(a, np.asarray(b, dtype=np.float64))


def explicit_ldl_dense(a: np.ndarray):
    """Textbook dense LDL^T (independent implementation)."""
    n = a.shape[0]
    l = np.eye(n)
    d = np.zeros(n)
    for j in range(n):
        s = a[j, j] - sum(l[j, k] ** 2 * d[k] for k in range(j))
        d[j] = s
        for i in range(j + 1, n):
            t = a[i, j] - sum(l[i, k] * l[j, k] * d[k]
                              for k in range(j))
            l[i, j] = t / s if s != 0 else np.nan
    return l, d


@pytest.fixture
def oracles():
    return type("Oracles", (), {
        "dense_bool_fill": staticmethod(dense_bool_fill),
        "dense_solve": staticmethod(dense_oracle_solve),
        "explicit_ldl": staticmethod(explicit_ldl_dense),
    })
