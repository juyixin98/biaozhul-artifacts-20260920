"""BLAS-backed numeric primitives.

The error bounds need ``sum(|x_i|)`` over the finite elements.  SciPy
exposes the canonical BLAS-1 routine ``dasum`` for exactly that; NumPy is
kept as a fallback so the package still imports on builds without SciPy's
compiled BLAS.  The value feeds only *a-priori* error bounds (loose by a
factor of n*u), so the single-ulp difference between reduction orders is
immaterial here - it is never used where exact summation is required.
"""
from __future__ import annotations

import numpy as np

try:
    from scipy.linalg.blas import dasum as _blas_dasum

    _HAVE_BLAS = True
except Exception:  # pragma: no cover - exercised only without scipy BLAS
    _HAVE_BLAS = False


def sum_abs(values: np.ndarray) -> float:
    """Return ``sum(|x_i|)`` over the finite elements as a Python float."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return 0.0
    if _HAVE_BLAS:
        # dasum requires a contiguous C-order float64 array.
        return float(_blas_dasum(np.ascontiguousarray(finite, dtype=np.float64)))
    return float(np.sum(np.abs(finite)))
