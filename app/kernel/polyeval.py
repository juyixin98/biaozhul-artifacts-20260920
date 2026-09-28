"""Shared polynomial evaluation helpers (Horner, descending powers)."""

from __future__ import annotations

import numpy as np


def poly_eval(coeffs: np.ndarray, z: np.ndarray) -> np.ndarray:
    """Evaluate p at each point of array z (Horner, descending powers)."""
    out = np.full(z.shape, coeffs[0], dtype=np.complex128)
    for c in coeffs[1:]:
        out = out * z + c
    return out


def poly_derivative_coeffs(coeffs: np.ndarray) -> np.ndarray:
    """Coefficients of p'(z), descending powers."""
    n = len(coeffs) - 1
    return np.array([coeffs[k] * (n - k) for k in range(n)], dtype=np.complex128)


def poly_from_roots(roots: np.ndarray) -> np.ndarray:
    """Monic polynomial coefficients (descending) from roots, by convolution.

    Implemented directly (not via numpy.polynomial) so the reconstruction
    path in the evidence layer is explicit and version-stable.
    """
    coeffs = np.array([1.0 + 0.0j])
    for r in roots:
        coeffs = np.convolve(coeffs, np.array([1.0 + 0.0j, -r]))
    return coeffs
