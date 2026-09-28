"""Tests for the error-evidence layer (sparse residual, reconstruction, HP)."""
from __future__ import annotations

import numpy as np
import pytest
from scipy import sparse

from config.settings import FactorizationConfig
from sparse_cholesky.core import FactorizationEngine, permute_matrix
from sparse_cholesky.evidence import (
    MAX_HP_ORDER,
    HighPrecisionTooLargeError,
    high_precision_evidence,
    high_precision_ldlt_solve,
    reconstruction_evidence,
    residual_evidence,
)

CONFIG = FactorizationConfig()


@pytest.mark.parametrize("ordering", ["natural", "rcm"])
def test_sparse_residual_is_small_for_correct_solution(spd_cases, ordering):
    engine = FactorizationEngine(CONFIG)
    rng = np.random.default_rng(99)
    for matrix in spd_cases:
        x_true = rng.standard_normal(matrix.n)
        b = matrix.csc @ x_true
        _, x = engine.factor(matrix, ordering=ordering, b=b)
        ev = residual_evidence(matrix.csc, x, b)
        assert ev.relative_residual_inf < 1e-11
        assert ev.relative_residual_2 < 1e-11
        assert 0 <= ev.max_residual_component < matrix.n


def test_residual_detects_a_wrong_solution(spd_cases):
    matrix = spd_cases[0]
    rng = np.random.default_rng(3)
    b = rng.standard_normal(matrix.n)
    wrong = rng.standard_normal(matrix.n)  # not a solution
    ev = residual_evidence(matrix.csc, wrong, b)
    assert ev.relative_residual_inf > 1e-3


def test_reconstruction_reports_zero_structural_mismatch(spd_cases):
    engine = FactorizationEngine(CONFIG)
    for matrix in spd_cases:
        result, _ = engine.factor(matrix)
        a_perm = permute_matrix(matrix.csc, result.permutation)
        rec = reconstruction_evidence(a_perm, result.numeric)
        # L pattern must equal symbolic prediction exactly.
        assert rec.factor_pattern_mismatches == 0
        # Fill cancellations at A's structural zeros must be at roundoff level.
        assert rec.max_cancellation < 1e-10
        assert rec.relative_fro < 1e-12


def test_reconstruction_detects_tampered_factor(spd_cases):
    engine = FactorizationEngine(CONFIG)
    result, _ = engine.factor(spd_cases[0])
    # Corrupt one pivot: reconstruction must diverge numerically.
    result.numeric.diag[0] *= 1.0001
    matrix = spd_cases[0]
    a_perm = permute_matrix(matrix.csc, result.permutation)
    rec = reconstruction_evidence(a_perm, result.numeric)
    assert rec.relative_fro > 1e-6


def test_high_precision_oracle_agrees_with_float64_kernel(spd_cases):
    engine = FactorizationEngine(CONFIG)
    matrix = spd_cases[0]  # tridiagonal n=20, within HP size bound
    rng = np.random.default_rng(7)
    x_true = rng.standard_normal(matrix.n)
    b = matrix.csc @ x_true
    _, x = engine.factor(matrix, b=b)
    hp = high_precision_evidence(matrix.csc.toarray(), b, x, dps=40)
    assert hp.forward_error_inf < 1e-12
    assert hp.reference_residual_inf < 1e-30
    assert hp.dps == 40
    assert len(hp.x_excerpt) == 5


def test_high_precision_solve_matches_dense_reference(spd_cases, ref):
    matrix = spd_cases[1]
    rng = np.random.default_rng(11)
    b = rng.standard_normal(matrix.n)
    x_hp = high_precision_ldlt_solve(matrix.csc.toarray(), b, dps=50)
    x_ref = ref.cholesky_solve(matrix.csc.toarray(), b)
    assert np.allclose(x_hp, x_ref, atol=1e-12, rtol=1e-12)


def test_high_precision_rejects_large_matrix():
    big = np.eye(MAX_HP_ORDER + 1)
    with pytest.raises(HighPrecisionTooLargeError):
        high_precision_ldlt_solve(big, np.zeros(big.shape[0]))


def test_high_precision_rejects_indefinite():
    a = np.array([[1.0, 2.0], [2.0, 1.0]])  # determinant -3 -> indefinite
    with pytest.raises(ValueError):
        high_precision_ldlt_solve(a, np.ones(2))
