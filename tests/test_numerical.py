"""Numerical LDL^T kernel: concrete values vs independent dense oracle,
reconstruction, and permutation applied to both ends."""
from __future__ import annotations

import numpy as np

from app.core import ordering as ordering_mod
from app.core.numerical import ldlt_factor, solve_original
from app.core.symbolic import symbolic_factor
from app.evidence.metrics import full_symmetric, permute
from app.numerical_input.fixtures import (
    arrowhead, banded, grid_laplacian)
from app.numerical_input.sparse_matrix import from_coo


def _factor(fx, method="minimum_degree"):
    si = from_coo(fx.n, fx.rows, fx.cols, fx.vals)
    coo = si.upper.tocoo()
    order = ordering_mod.compute_ordering(method, coo.row, coo.col, si.n)
    sym = symbolic_factor(si.upper, order)
    b = permute(full_symmetric(si.upper), order.perm)
    return si, sym, ldlt_factor(b, sym)


def test_diagonal_values_match_dense_ldl_oracle(oracles):
    fx = grid_laplacian(5, 4)
    si, sym, fac = _factor(fx, "natural")
    a_dense = full_symmetric(si.upper).toarray()
    perm = sym.ordering.perm
    l_ref, d_ref = oracles.explicit_ldl(a_dense[perm, :][:, perm])
    np.testing.assert_allclose(fac.diag, d_ref, rtol=1e-10, atol=1e-12)


def test_solution_matches_lapack_each_fixture(all_spd_fixtures, oracles):
    for fx in all_spd_fixtures:
        si, sym, fac = _factor(fx, "minimum_degree")
        x = solve_original(fac, fx.rhs)
        x_ref = oracles.dense_solve(
            fx.rows, fx.cols, fx.vals, fx.n, fx.rhs)
        np.testing.assert_allclose(
            x, x_ref, rtol=1e-8, atol=1e-10,
            err_msg=f"solution mismatch on {fx.name}")


def test_reconstruction_l_d_lt_equals_permuted_a():
    fx = grid_laplacian(6, 5)
    si, sym, fac = _factor(fx, "nested_dissection")
    import scipy.sparse as sp
    l = fac.as_csc()
    rebuilt = (l @ sp.diags(fac.diag) @ l.T).toarray()
    b = permute(full_symmetric(si.upper), sym.ordering.perm).toarray()
    np.testing.assert_allclose(rebuilt, b, atol=1e-9, rtol=1e-9)


def test_permutation_applied_to_rhs():
    # A deliberately non-symmetric RHS; verify PAP^T x-perm consistency
    # by checking the residual in the ORIGINAL ordering.
    fx = arrowhead(12)
    si, sym, fac = _factor(fx, "minimum_degree")
    rhs = np.array([(k + 1) ** 2 * (-1) ** k for k in range(fx.n)],
                   dtype=float)
    x = solve_original(fac, rhs)
    a = full_symmetric(si.upper)
    r = a @ x - rhs
    assert np.linalg.norm(r, ord=np.inf) < 1e-8


def test_unit_diagonal_of_l():
    fx = banded(20)
    _si, sym, fac = _factor(fx, "minimum_degree")
    for k in range(fx.n):
        # first stored entry of each column is the pivot row, value 1
        assert fac.l_values[fac.l_indptr[k]] == 1.0
        assert fac.l_indices[fac.l_indptr[k]] == k


def test_multiple_rhs_same_factor_consistency():
    fx = grid_laplacian(5, 5)
    _si, sym, fac = _factor(fx, "minimum_degree")
    rng = np.random.default_rng(7)
    for _ in range(3):
        rhs = rng.standard_normal(fx.n)
        x = solve_original(fac, rhs)
        # the symbolic/numeric factor is reused; residual must stay tiny
        a = full_symmetric(_si.upper)
        assert np.linalg.norm(a @ x - rhs, np.inf) < 1e-8
