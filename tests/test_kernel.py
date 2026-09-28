"""Kernel-level tests for Householder tridiagonalization + implicit QR.

These tests assert concrete numerical results, not merely that the interface
runs. Independent answers come from NumPy/SciPy, never from this kernel.
"""

import numpy as np
import pytest

from sym_eig.config import FLOAT64_EPS
from sym_eig.numerical.kernel import (
    NonConvergenceError,
    householder_tridiagonal,
    implicit_wilkinson_qr,
    symmetric_eigen,
)


def test_diagonal_matrix_returns_entries_and_standard_basis():
    lam = np.array([-3.0, -1.0, 0.5, 2.0, 7.0])
    result = symmetric_eigen(np.diag(lam), max_iters=30)

    np.testing.assert_allclose(result.eigenvalues, np.sort(lam), atol=1e-14)
    expected_basis = np.eye(5)[:, np.argsort(lam)]
    # Eigenvectors of a diagonal matrix are fixed up to sign.
    overlap = np.abs(np.diag(result.eigenvectors.T @ expected_basis))
    np.testing.assert_allclose(overlap, np.ones(5), atol=1e-14)
    assert result.qr_sweeps == 0  # already diagonal: no QR sweep needed


def test_tridiagonalization_preserves_orthogonal_similarity(rng):
    a = _random_symmetric(rng, 10)
    tri = householder_tridiagonal(a)
    q, t = tri.orthogonal, np.diag(tri.diagonal)
    t += np.diag(tri.offdiagonal[:-1], 1)
    t += np.diag(tri.offdiagonal[:-1], -1)

    np.testing.assert_allclose(q @ t @ q.T, a, atol=1e-12)
    np.testing.assert_allclose(q.T @ q, np.eye(10), atol=1e-14)


def test_random_matrices_match_numpy_across_sizes_and_scales(rng):
    worst = 0.0
    for n in (2, 3, 5, 9, 16, 27):
        a = _random_symmetric(rng, n)
        scale = 10.0 ** int(rng.integers(-6, 7))
        result = symmetric_eigen(a * scale, max_iters=60)
        reference = np.linalg.eigvalsh(a * scale)
        rel = np.max(np.abs(result.eigenvalues - reference)) / max(
            1.0, np.max(np.abs(reference))
        )
        residual = np.linalg.norm(
            a * scale @ result.eigenvectors - result.eigenvectors * result.eigenvalues
        ) / np.linalg.norm(a * scale)
        worst = max(worst, rel, residual)
        assert rel < 1e-10
        assert residual < 1e-11
    assert worst < 1e-9


def test_repeated_eigenvalues_exact_spectrum_and_orthonormal_space(rng):
    eigenvalues = np.array([1.0, 1.0, 1.0, 2.0, 3.5, 3.5, 5.0])
    a = _spectrum_matrix(rng, eigenvalues)
    result = symmetric_eigen(a, max_iters=30)

    np.testing.assert_allclose(result.eigenvalues, eigenvalues, atol=1e-12)
    np.testing.assert_allclose(
        result.eigenvectors.T @ result.eigenvectors,
        np.eye(7),
        atol=1e-12,
    )
    # The triple eigenspace itself must satisfy A V = V*1 even though no
    # individual vector is canonical.
    triple = result.eigenvectors[:, :3]
    np.testing.assert_allclose(a @ triple, triple, atol=1e-12)


def test_widely_separated_scales(rng):
    eigenvalues = np.array([-1.0e8, 1.0, 1.0e-6])
    a = _spectrum_matrix(rng, eigenvalues, seed=7)
    result = symmetric_eigen(a, max_iters=30)
    # The nominal 1e-6 eigenvalue cannot be represented more accurately in A
    # than the float64 ulp at 1e8 (~1e-8); compare to the best float64 answer
    # (NumPy on the same input), not to the nominal construction spectrum.
    np.testing.assert_allclose(
        result.eigenvalues, np.linalg.eigvalsh(a), atol=1e-10
    )


def test_exhausted_budget_raises_and_does_not_pretend_convergence():
    n = 30
    off = np.ones(n - 1)
    laplacian = 2.0 * np.eye(n) - np.diag(off, 1) - np.diag(off, -1)
    tri = householder_tridiagonal(laplacian)
    with pytest.raises(NonConvergenceError) as excinfo:
        implicit_wilkinson_qr(
            tri.diagonal, tri.offdiagonal, tri.orthogonal, max_iters=1
        )
    assert excinfo.value.sweeps == 1
    assert excinfo.value.block[0] < excinfo.value.block[1]
    # The unreduced off-diagonal is demonstrably still far from deflation.
    assert excinfo.value.residual_offdiag > FLOAT64_EPS


def test_same_matrix_converges_when_budget_allows():
    n = 30
    off = np.ones(n - 1)
    laplacian = 2.0 * np.eye(n) - np.diag(off, 1) - np.diag(off, -1)
    result = symmetric_eigen(laplacian, max_iters=30)
    np.testing.assert_allclose(
        result.eigenvalues, np.linalg.eigvalsh(laplacian), rtol=1e-12
    )
    assert result.max_block_sweeps <= 30


def test_iteration_budget_is_configurable():
    off = np.ones(4)
    small = 2.0 * np.eye(5) - np.diag(off, 1) - np.diag(off, -1)
    with pytest.raises(NonConvergenceError):
        symmetric_eigen(small, max_iters=1)
    result = symmetric_eigen(small, max_iters=20)
    assert result.qr_sweeps >= 1
    np.testing.assert_allclose(
        result.eigenvalues, np.linalg.eigvalsh(small), atol=1e-12
    )


def test_one_by_one_matrix():
    result = symmetric_eigen(np.array([[4.25]]), max_iters=30)
    assert result.eigenvalues.tolist() == pytest.approx([4.25])
    assert abs(abs(result.eigenvectors[0, 0]) - 1.0) < 1e-15


def _random_symmetric(rng: np.random.Generator, n: int) -> np.ndarray:
    m = rng.standard_normal((n, n))
    return m + m.T


def _spectrum_matrix(
    rng: np.random.Generator, eigenvalues: np.ndarray, seed: int | None = None
) -> np.ndarray:
    if seed is not None:
        rng = np.random.default_rng(seed)
    q = np.linalg.qr(rng.standard_normal((len(eigenvalues),) * 2))[0]
    return (q * eigenvalues) @ q.T
