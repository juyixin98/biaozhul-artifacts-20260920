"""Numerical evidence measures.

These functions are deliberately independent of how the eigenvalues were
computed: they only see ``A`` and the returned ``(w, V)``. The kernel never
certifies itself.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class EvidenceMeasures:
    """Raw verification measures (all relative where a scale exists)."""

    residual_fro: float
    """||A V - V diag(w)||_F / max(1, ||A||_F) - the eigen-equation residual
    (the max(1,.) floor means this is an absolute quantity for ||A||_F < 1)."""

    residual_max_per_eigen: float
    """Worst single-vector residual ||A v_i - w_i v_i|| / max(1, ||A||_F)."""

    orthogonality_fro: float
    """||V^T V - I||_F - deviation of eigenvectors from an orthonormal basis."""

    orthogonality_max: float
    """max |V^T V - I| - catches a single bad pair of vectors."""

    reconstruction_rel: float
    """||A - V diag(w) V^T||_F / max(1, ||A||_F) - spectral reconstruction
    error (absolute when ||A||_F < 1)."""

    matrix_scale: float
    """||A||_F, reported so relative measures stay interpretable."""


def measure(A: np.ndarray, w: np.ndarray, V: np.ndarray) -> EvidenceMeasures:
    a_norm = float(np.linalg.norm(A, "fro"))
    denom = max(a_norm, 1.0)
    av = A @ V
    residual_matrix = av - V * w
    per_eigen = np.linalg.norm(residual_matrix, axis=0) / denom
    gram = V.T @ V
    identity = np.eye(V.shape[1])
    gram_error = gram - identity
    reconstructed = V @ ((V * w).T)  # V diag(w) V^T, without forming diag
    return EvidenceMeasures(
        residual_fro=float(np.linalg.norm(residual_matrix, "fro") / denom),
        residual_max_per_eigen=float(np.max(per_eigen)),
        orthogonality_fro=float(np.linalg.norm(gram_error, "fro")),
        orthogonality_max=float(np.max(np.abs(gram_error))),
        reconstruction_rel=float(
            np.linalg.norm(A - reconstructed, "fro") / denom
        ),
        matrix_scale=a_norm,
    )
