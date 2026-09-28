"""Shared pytest fixtures and *independent* reference implementations.

The references here are deliberately written from scratch using dense NumPy /
Python and never call the sparse core; otherwise the tests would "verify"
the implementation against itself.

References
----------
``dense_cholesky_solve``  numpy.linalg.cholesky based solution (float64)
``dense_ldlt``            textbook dense LDL^T (pivot list)
``dense_bool_chol_pattern`` boolean elimination predicting fill exactly
``pivot_of_dense_ldlt``   first non-positive pivot index of a dense matrix
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sparse_cholesky.input.fixtures import (  # noqa: E402
    banded_spd,
    grid2d_laplacian,
    non_positive_definite_fixture,
    pattern_change_pair,
    spd_with_dense_block,
    tridiagonal_spd,
)
from sparse_cholesky.input.matrix import build_sparse_matrix  # noqa: E402


# ---------------------------------------------------------------------------
# Independent dense references (NOT using the sparse core)
# ---------------------------------------------------------------------------


def dense_cholesky_solve(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Reference solve: numpy's dense Cholesky followed by hand-written
    triangular substitution (independent of the sparse kernels)."""
    l_chol = np.linalg.cholesky(a)
    y = _fwd(l_chol, b)
    return _back(l_chol.T, y)


def _fwd(l: np.ndarray, b: np.ndarray) -> np.ndarray:
    n = l.shape[0]
    y = np.array(b, dtype=np.float64, copy=True)
    for i in range(n):
        for j in range(i):
            y[i] -= l[i, j] * y[j]
        y[i] /= l[i, i]
    return y


def _back(u: np.ndarray, y: np.ndarray) -> np.ndarray:
    n = u.shape[0]
    x = np.array(y, dtype=np.float64, copy=True)
    for i in range(n - 1, -1, -1):
        for j in range(i + 1, n):
            x[i] -= u[i, j] * x[j]
        x[i] /= u[i, i]
    return x


def dense_ldlt(a: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Textbook dense LDL^T; returns (L unit lower, D diagonal).

    On a non-positive pivot the column is left as zero (no division by a bad
    pivot) so singular/indefinite references can still be inspected without
    emitting divide-by-zero warnings.
    """
    n = a.shape[0]
    l = np.eye(n)
    d = np.zeros(n)
    for k in range(n):
        d[k] = a[k, k] - sum(d[j] * l[k, j] ** 2 for j in range(k))
        if d[k] <= 0.0:
            l[k + 1:, k] = 0.0
            continue
        for i in range(k + 1, n):
            l[i, k] = (a[i, k] - sum(d[j] * l[i, j] * l[k, j]
                                     for j in range(k))) / d[k]
    return l, d


def dense_ldlt_pivots(a: np.ndarray) -> np.ndarray:
    """Full pivot sequence of a dense LDL^T (values may be <= 0)."""
    return dense_ldlt(a)[1]


def first_nonpositive_pivot(a: np.ndarray) -> int:
    """Index of the first non-positive dense LDL^T pivot."""
    _, d = dense_ldlt(a)
    bad = np.flatnonzero(d <= 0.0)
    return int(bad[0]) if bad.size else -1


def dense_bool_chol_pattern(a: np.ndarray) -> np.ndarray:
    """Predict the exact lower Cholesky pattern by dense boolean elimination."""
    n = a.shape[0]
    support = (a != 0.0).astype(bool)
    for k in range(n):
        later = [i for i in range(k + 1, n) if support[i, k]]
        # All members of "later" become pairwise connected (clique fill).
        for i in later:
            support[i, later] = True
    return np.tril(support)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ref():
    """Bundle of independent reference helpers."""
    class Refs:
        cholesky_solve = staticmethod(dense_cholesky_solve)
        ldlt = staticmethod(dense_ldlt)
        ldlt_pivots = staticmethod(dense_ldlt_pivots)
        first_nonpositive_pivot = staticmethod(first_nonpositive_pivot)
        bool_pattern = staticmethod(dense_bool_chol_pattern)
    return Refs()


@pytest.fixture
def spd_cases():
    """A spread of SPD sparse cases: tridiagonal, banded, grid, dense block."""
    builders = [
        tridiagonal_spd(20),
        banded_spd(25, 4),
        grid2d_laplacian(5),
        spd_with_dense_block(6, 4),
    ]
    return [build_sparse_matrix(*fx.keys()) for fx in builders]


@pytest.fixture
def nonpd_cases():
    return {
        kind: build_sparse_matrix(*non_positive_definite_fixture(kind, n=8).keys())
        for kind in ("zero_pivot", "negative_diag", "indefinite")
    }


@pytest.fixture
def pattern_pair():
    a, b = pattern_change_pair()
    return build_sparse_matrix(*a.keys()), build_sparse_matrix(*b.keys())
