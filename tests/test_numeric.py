"""Tests for numeric LDL^T factorization and triangular solves."""
from __future__ import annotations

import numpy as np
import pytest
from scipy import sparse

from config.settings import FactorizationConfig
from sparse_cholesky.core import FactorizationEngine
from sparse_cholesky.core.numeric import numeric_factorization
from sparse_cholesky.core.ordering import (
    compute_ordering,
    permute_matrix,
    permute_rhs,
    unpermute_solution,
)
from sparse_cholesky.core.symbolic import symbolic_factorization
from sparse_cholesky.input.errors import (
    NonPositiveDefiniteError,
    PivotTooSmallError,
)

CONFIG = FactorizationConfig()


@pytest.mark.parametrize("ordering", ["natural", "rcm"])
def test_solution_matches_dense_cholesky_reference(spd_cases, ref, ordering):
    engine = FactorizationEngine(CONFIG)
    rng = np.random.default_rng(2026)
    for matrix in spd_cases:
        dense_a = matrix.csc.toarray()
        x_true = rng.standard_normal(matrix.n)
        b = dense_a @ x_true
        _, x = engine.factor(matrix, ordering=ordering, b=b)
        x_ref = ref.cholesky_solve(dense_a, b)
        rel_err = np.linalg.norm(x - x_ref, np.inf) / np.linalg.norm(x_ref, np.inf)
        assert rel_err < 1e-10, (
            f"n={matrix.n} ordering={ordering} rel_err={rel_err}"
        )


@pytest.mark.parametrize("ordering", ["natural", "rcm"])
def test_reconstruction_matches_original_matrix(spd_cases, ordering):
    engine = FactorizationEngine(CONFIG)
    for matrix in spd_cases:
        result, _ = engine.factor(matrix, ordering=ordering)
        perm = result.permutation.perm
        a_perm = matrix.csc[perm, :][:, perm]
        l_mat = result.numeric.l_csc()
        d_mat = sparse.diags(result.numeric.diag)
        rebuilt = (l_mat @ d_mat @ l_mat.T).tocsc()
        rel_fro = sparse.linalg.norm(rebuilt - a_perm, "fro") / sparse.linalg.norm(
            a_perm, "fro"
        )
        assert rel_fro < 1e-12


def test_pivots_match_dense_ldlt_pivots(spd_cases, ref):
    engine = FactorizationEngine(CONFIG)
    for matrix in spd_cases:
        result, _ = engine.factor(matrix, ordering="natural")
        _, d_ref = ref.ldlt(matrix.csc.toarray())
        assert np.allclose(result.pivots, d_ref, rtol=1e-10, atol=1e-10)


def test_cholesky_factor_satisfies_llt(spd_cases):
    engine = FactorizationEngine(CONFIG)
    matrix = spd_cases[1]  # banded
    result, _ = engine.factor(matrix)
    lc = result.numeric.cholesky_csc()
    perm = result.permutation.perm
    a_perm = matrix.csc[perm, :][:, perm]
    err = sparse.linalg.norm((lc @ lc.T - a_perm), "fro") / sparse.linalg.norm(
        a_perm, "fro"
    )
    assert err < 1e-12


@pytest.mark.parametrize("kind", ["zero_pivot", "negative_diag", "indefinite"])
def test_non_positive_definite_localizes_pivot(nonpd_cases, ref, kind):
    matrix = nonpd_cases[kind]
    engine = FactorizationEngine(CONFIG)
    with pytest.raises(NonPositiveDefiniteError) as exc:
        engine.factor(matrix, ordering="natural")
    err = exc.value
    assert err.error_type == "non_positive_definite_error"
    assert err.pivot_index is not None
    assert err.pivot_value <= 0.0 or not np.isfinite(err.pivot_value)
    # The reported index must agree with the independent dense reference.
    expected = ref.first_nonpositive_pivot(matrix.csc.toarray())
    assert err.pivot_index == expected
    # Natural ordering => permuted and original indices coincide.
    assert err.ordering_index == expected


def test_negative_pivot_value_reported_under_rcm(nonpd_cases):
    # Under a permutation the original (pre-ordering) index is also reported.
    matrix = nonpd_cases["negative_diag"]
    engine = FactorizationEngine(CONFIG)
    with pytest.raises(NonPositiveDefiniteError) as exc:
        engine.factor(matrix, ordering="rcm")
    err = exc.value
    assert err.pivot_index >= 0
    assert err.ordering_index is not None
    assert 0 <= err.ordering_index < matrix.n


def test_pivot_too_small_is_distinct_failure_category():
    # Build an SPD-but-nearly-singular matrix whose small pivot is positive.
    n = 3
    a = np.array([[1e-20, 1e-11, 0.0],
                  [1e-11, 1.0, 0.0],
                  [0.0, 0.0, 1.0]])
    # Symmetrize and ensure SPD (tiny leading entry).
    a = (a + a.T) / 2
    rows, cols, vals = [], [], []
    for i in range(n):
        for j in range(i + 1):
            if a[i, j] != 0.0:
                rows.append(i); cols.append(j); vals.append(a[i, j])
    from sparse_cholesky.input.matrix import build_sparse_matrix
    matrix = build_sparse_matrix(n, rows, cols, vals)
    strict = FactorizationConfig(pivot_tol_abs=1e-8, pivot_tol_rel=0.0)
    engine = FactorizationEngine(strict)
    with pytest.raises(PivotTooSmallError) as exc:
        engine.factor(matrix)
    assert exc.value.error_type == "pivot_too_small_error"
    assert exc.value.pivot_value > 0.0


def test_rhs_permutation_is_symmetric_with_matrix():
    # If permutation only touched one side, this identity would fail.
    engine = FactorizationEngine(CONFIG)
    from sparse_cholesky.input.fixtures import grid2d_laplacian
    from sparse_cholesky.input.matrix import build_sparse_matrix

    matrix = build_sparse_matrix(*grid2d_laplacian(4).keys())
    rng = np.random.default_rng(1)
    x_true = rng.standard_normal(matrix.n)
    b = matrix.csc @ x_true
    result, x = engine.factor(matrix, ordering="rcm", b=b)
    # Solution returned in original coordinates must satisfy the original A.
    residual = np.linalg.norm(matrix.csc @ x - b, np.inf)
    assert residual < 1e-9


def test_permute_and_unpermute_roundtrip():
    from sparse_cholesky.input.fixtures import banded_spd
    from sparse_cholesky.input.matrix import build_sparse_matrix

    matrix = build_sparse_matrix(*banded_spd(10, 2).keys())
    perm = compute_ordering("rcm", matrix.csc)
    b = np.arange(matrix.n, dtype=float)
    b_perm = permute_rhs(b, perm)
    assert np.array_equal(b_perm, b[perm.perm])
    # Solving a permuted identity relationship: unpermute restores order.
    y = b_perm.copy()
    x = unpermute_solution(y, perm)
    assert np.allclose(x, b)
