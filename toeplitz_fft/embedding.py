"""Circulant embedding for Toeplitz matrices.

A Toeplitz matrix T (m x n) is defined by its first column ``c`` (length m)
and first row ``r`` (length n) with the convention T[i, j] = c[i - j] for
i >= j and T[i, j] = r[j - i] for i < j. The element T[0, 0] is shared, so
``c[0]`` and ``r[0]`` must agree; this module enforces that invariant.

T is embedded into an L x L circulant matrix C with L >= m + n - 1 so that
the linear convolution computed via the FFT cannot wrap around (no circular
aliasing). The first column of C is

    [ c[0], ..., c[m-1], 0, ..., 0, r[n-1], ..., r[1] ]

and T @ x equals the first m entries of C @ (x zero-padded to length L).
"""

from __future__ import annotations

import numpy as np

from .errors import InconsistentToeplitzError, ShapeMismatchError


def embedding_length(m: int, n: int, pad_to_power_of_two: bool = False) -> int:
    """Return the circulant embedding length L.

    L = m + n - 1 is the minimal length that avoids circular aliasing.
    With ``pad_to_power_of_two`` the length is rounded up to the next power
    of two for FFT efficiency; it never drops below m + n - 1.
    """
    if m < 1 or n < 1:
        raise ShapeMismatchError(f"Toeplitz dimensions must be >= 1, got m={m}, n={n}")
    length = m + n - 1
    if pad_to_power_of_two and length > 1:
        length = 1 << (length - 1).bit_length()
    return length


def check_shared_element(c0: complex, r0: complex, rtol: float, atol: float) -> None:
    """Enforce the shared-element invariant c[0] == r[0]."""
    if not np.isclose(c0, r0, rtol=rtol, atol=atol):
        raise InconsistentToeplitzError(
            f"first column c[0]={c0!r} and first row r[0]={r0!r} disagree on the "
            f"shared element T[0,0] (rtol={rtol}, atol={atol})"
        )


def validate_first_column_row(
    c: np.ndarray, r: np.ndarray, rtol: float, atol: float
) -> None:
    """Validate shapes and the shared element of a (c, r) Toeplitz description."""
    if c.ndim != 1 or r.ndim != 1:
        raise ShapeMismatchError(
            f"first column and first row must be 1-D, got ndim {c.ndim} and {r.ndim}"
        )
    if c.size < 1 or r.size < 1:
        raise ShapeMismatchError("first column and first row must be non-empty")
    check_shared_element(c[0], r[0], rtol, atol)


def circulant_first_column(c: np.ndarray, r: np.ndarray, L: int) -> np.ndarray:
    """Build the first column of the embedding circulant matrix.

    The result has length L with c in positions [0, m), zeros in the gap
    [m, L - n + 1), and r[1:] reversed in positions [L - n + 1, L).
    """
    m, n = c.shape[0], r.shape[0]
    if L < m + n - 1:
        raise ShapeMismatchError(
            f"embedding length L={L} too small for m={m}, n={n} (need >= {m + n - 1})"
        )
    dtype = np.result_type(c, r)
    col = np.zeros(L, dtype=dtype)
    col[:m] = c
    if n > 1:
        col[L - (n - 1):] = r[1:][::-1]
    return col
