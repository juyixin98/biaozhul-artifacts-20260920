"""内核单元测试: Householder 三对角化与隐式移位 QL。"""

from __future__ import annotations

import numpy as np
import pytest

from eigenservice.kernel import eigh_core, householder_tridiagonal, tridiagonal_ql

from .fixtures import (
    diagonal_matrix,
    random_symmetric,
    repeated_spectrum_matrix,
    with_spectrum,
)


@pytest.mark.unit
def test_householder_produces_tridiagonal_and_orthogonal_factor() -> None:
    matrix = random_symmetric(6, seed=11)
    diag, offdiag, q_acc = householder_tridiagonal(matrix)

    tri = np.diag(diag) + np.diag(offdiag, 1) + np.diag(offdiag, -1)
    # 三对角之外必须为零
    full = tri.copy()
    for i in range(6):
        for j in range(6):
            if abs(i - j) > 1:
                assert full[i, j] == 0.0
    # Q 正交且 A = Q T Q^T
    assert np.linalg.norm(q_acc.T @ q_acc - np.eye(6)) < 1e-12
    assert np.linalg.norm(q_acc @ tri @ q_acc.T - matrix) < 1e-12


@pytest.mark.unit
@pytest.mark.parametrize("n", [1, 2, 3, 5, 10, 25])
def test_core_matches_lapack_eigenvalues(n: int) -> None:
    matrix = random_symmetric(n, seed=700 + n)
    result = eigh_core(matrix, max_sweeps=max(30, 12 * n), eig_tol=1e-14)

    assert result.converged is True
    assert result.stalled_index is None
    expected = np.linalg.eigvalsh(matrix)
    np.testing.assert_allclose(result.eigenvalues, expected, atol=1e-10, rtol=1e-10)

    w, vec = result.eigenvalues, result.eigenvectors
    assert np.linalg.norm(matrix @ vec - vec * w) / np.linalg.norm(matrix) < 1e-10
    assert np.linalg.norm(vec.T @ vec - np.eye(n)) < 1e-10


@pytest.mark.unit
def test_diagonal_matrix_zero_sweeps() -> None:
    matrix = diagonal_matrix()
    result = eigh_core(matrix, max_sweeps=100, eig_tol=1e-14)
    assert result.converged is True
    # 已是三对角且次对角全零, 无需任何 QL 移位步
    assert result.sweeps == 0
    np.testing.assert_allclose(result.eigenvalues, [-3.0, -1.0, 0.5, 2.0, 7.0])


@pytest.mark.unit
def test_repeated_spectrum_converges_with_subspace() -> None:
    matrix = repeated_spectrum_matrix()
    result = eigh_core(matrix, max_sweeps=200, eig_tol=1e-14)
    assert result.converged is True
    np.testing.assert_allclose(
        result.eigenvalues, [-1.5, -1.5, 2.0, 2.0, 2.0], atol=1e-12
    )
    vec = result.eigenvectors
    assert np.linalg.norm(vec.T @ vec - np.eye(5)) < 1e-10


@pytest.mark.unit
def test_known_spectrum_constructed_matrix() -> None:
    spectrum = [-4.0, 0.0, 1.5, 9.0]
    matrix = with_spectrum(spectrum, seed=99)
    result = eigh_core(matrix, max_sweeps=200, eig_tol=1e-14)
    assert result.converged is True
    np.testing.assert_allclose(result.eigenvalues, sorted(spectrum), atol=1e-11)


@pytest.mark.unit
def test_budget_exhaustion_reports_not_converged() -> None:
    # 5x5 稠密矩阵在 0 预算下不可能收敛 —— 内核必须如实报告。
    matrix = random_symmetric(5, seed=12)
    diag, offdiag, q_acc = householder_tridiagonal(matrix)
    _d, _z, sweeps, converged, stalled = tridiagonal_ql(
        diag, offdiag, q_acc, max_sweeps=0, eig_tol=1e-14
    )
    assert converged is False
    assert stalled is not None
    assert sweeps == 1  # 尝试了一步即超预算
