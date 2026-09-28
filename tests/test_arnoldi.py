"""Arnoldi kernel properties: orthonormality, projection identity, breakdown."""

from __future__ import annotations

import numpy as np
import scipy.sparse as sp

from krylov_expv.core.arnoldi import arnoldi


def test_basis_is_orthonormal_and_hessenberg_relation_holds():
    rng = np.random.default_rng(7)
    A = sp.csr_matrix(rng.standard_normal((12, 12)))
    v = rng.standard_normal(12)
    m = 6
    result = arnoldi(lambda x: A @ x, v, m)

    V, H = result.basis, result.hessenberg
    assert V.shape == (12, m + 1)
    assert H.shape == (m + 1, m)
    # orthonormality of the basis columns actually used
    gram = V[:, :m].T @ V[:, :m]
    assert np.allclose(gram, np.eye(m), atol=1e-12)
    # Arnoldi relation A V_m = V_{m+1} H
    assert np.allclose(A @ V[:, :m], V @ H, atol=1e-12)
    # first column is the normalised start vector
    assert np.allclose(V[:, 0], v / np.linalg.norm(v))
    assert result.beta == np.linalg.norm(v)
    assert not result.happy_breakdown


def test_happy_breakdown_on_invariant_subspace():
    # block-diagonal: v lives entirely in the first 2x2 block, so the Krylov
    # subspace is at most 2-dimensional and Arnoldi must break down there.
    A = sp.csr_matrix(np.diag([1.0, 2.0, 5.0, 6.0]))
    v = np.array([3.0, -1.0, 0.0, 0.0])
    result = arnoldi(lambda x: A @ x, v, m_max=4)

    assert result.happy_breakdown
    assert result.dim == 2
    assert result.hessenberg[result.dim, result.dim - 1] == 0.0
    # with an invariant subspace the Krylov exponential is exact
    from krylov_expv.core.dense_expm import augmented_expm_action

    dense = augmented_expm_action(result.hessenberg[:2, :2], tau=0.8)
    w = result.beta * (result.basis[:, :2] @ dense.step)
    expected = np.array([3.0 * np.exp(0.8), -np.exp(1.6), 0.0, 0.0])
    assert np.allclose(w, expected, atol=1e-12)
