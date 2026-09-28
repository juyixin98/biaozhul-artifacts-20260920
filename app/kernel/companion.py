"""Companion-matrix kernel: roots as eigenvalues of the companion matrix."""

from __future__ import annotations

import numpy as np
import scipy.linalg

from ..domain import NormalizedPolynomial
from ..errors import ComputationFailedError


def companion_matrix(poly: NormalizedPolynomial) -> np.ndarray:
    """Companion matrix of the *monic* form of p (descending coefficients)."""
    c = poly.coeffs / poly.coeffs[0]
    n = poly.degree
    mat = np.zeros((n, n), dtype=np.complex128)
    mat[0, :] = -c[1:]
    if n > 1:
        mat[1:, :-1] = np.eye(n - 1)
    return mat


def companion_roots(poly: NormalizedPolynomial, *, run_id: str = None) -> np.ndarray:
    """All roots via a balanced eigendecomposition of the companion matrix."""
    mat = companion_matrix(poly)
    try:
        vals = scipy.linalg.eigvals(mat, check_finite=True)
    except (scipy.linalg.LinAlgError, ValueError) as exc:
        raise ComputationFailedError(
            "companion-matrix eigendecomposition failed",
            detail={"reason": str(exc)},
            run_id=run_id,
        ) from exc
    if not np.all(np.isfinite(vals)):
        raise ComputationFailedError(
            "companion-matrix eigendecomposition returned non-finite roots",
            run_id=run_id,
        )
    return vals
