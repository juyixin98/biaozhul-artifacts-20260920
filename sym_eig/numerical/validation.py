"""Numerical input validation at the system boundary.

Everything coming from JSON or a caller is treated as untrusted data and
validated here before it reaches the kernel: shape/type, finiteness, size
budget, an arithmetic-scale bound (so later float64 arithmetic cannot
overflow to Inf/NaN), and relative-tolerance symmetry.
"""

import math
from typing import Any

import numpy as np

from sym_eig.config import Settings
from sym_eig.errors import EigServiceError, ErrorCategory

# Safety factor guarding the Householder/QR intermediates: the largest
# temporary product is of order n * max|a_ij|^2 (dot products of length n);
# a factor of 8 margin keeps those, the symmetrization (2*max), and a few
# downstream products strictly finite.
_ARITHMETIC_SAFETY_FACTOR = 8.0


def to_finite_matrix(raw: Any, settings: Settings) -> np.ndarray:
    """Validate an externally supplied matrix and return a float64 ndarray.

    Raises :class:`EigServiceError` with a precise category on any defect.
    """
    if not isinstance(raw, list) or len(raw) == 0:
        raise EigServiceError(
            ErrorCategory.INVALID_MATRIX,
            "matrix must be a non-empty JSON array of rows",
        )
    width: int | None = None
    converted: list[list[float]] = []
    for i, row in enumerate(raw):
        if not isinstance(row, list) or len(row) == 0:
            raise EigServiceError(
                ErrorCategory.INVALID_MATRIX,
                f"row {i} is not a non-empty array",
                {"row_index": i},
            )
        if width is None:
            width = len(row)
        elif len(row) != width:
            raise EigServiceError(
                ErrorCategory.INVALID_MATRIX,
                f"ragged matrix: row {i} has length {len(row)}, expected {width}",
                {"row_index": i, "length": len(row), "expected": width},
            )
        out_row: list[float] = []
        for j, value in enumerate(row):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EigServiceError(
                    ErrorCategory.INVALID_MATRIX,
                    f"entry [{i},{j}] is not a real number",
                    {"row_index": i, "column_index": j},
                )
            try:
                number = float(value)
            except OverflowError:
                # A finite Python integer larger than float64 range.
                raise EigServiceError(
                    ErrorCategory.VALUE_OUT_OF_RANGE,
                    f"entry [{i},{j}] is outside the float64 range; "
                    "rescale the matrix",
                    {"row_index": i, "column_index": j},
                ) from None
            out_row.append(number)
        converted.append(out_row)

    assert width is not None
    n = len(raw)
    if width != n:
        raise EigServiceError(
            ErrorCategory.INVALID_MATRIX,
            f"matrix is not square: {n} rows and {width} columns",
            {"rows": n, "columns": width},
        )
    if n > settings.max_n:
        raise EigServiceError(
            ErrorCategory.SIZE_LIMIT_EXCEEDED,
            f"matrix dimension {n} exceeds configured limit {settings.max_n}",
            {"n": n, "max_n": settings.max_n},
        )

    matrix = np.asarray(converted, dtype=np.float64)
    if not np.all(np.isfinite(matrix)):
        bad = np.argwhere(~np.isfinite(matrix))[0]
        raise EigServiceError(
            ErrorCategory.INVALID_MATRIX,
            f"entry [{bad[0]},{bad[1]}] is NaN or infinite",
            {"row_index": int(bad[0]), "column_index": int(bad[1])},
        )

    # Arithmetic-scale bound: every entry must be small enough that the
    # Householder dot products / rank-2 updates and the symmetrization cannot
    # overflow. Entries that are individually finite (e.g. 1e300) can still
    # make v@v or (A + A^T) infinite, which would otherwise leak NaN/Inf into
    # the result. Such a matrix must be rescaled first.
    fmax = np.finfo(np.float64).max
    max_abs = float(np.max(np.abs(matrix))) if matrix.size else 0.0
    entry_limit = math.sqrt(fmax / (_ARITHMETIC_SAFETY_FACTOR * max(1, n)))
    if not (math.isfinite(max_abs) and 2.0 * max_abs <= fmax
            and max_abs <= entry_limit):
        raise EigServiceError(
            ErrorCategory.VALUE_OUT_OF_RANGE,
            "matrix entries are too large in magnitude for safe float64 "
            f"arithmetic (max |a_ij| = {max_abs:.3e}, safe limit "
            f"{entry_limit:.3e}); rescale the matrix by a constant factor",
            {"max_abs": max_abs, "safe_entry_limit": entry_limit},
        )
    return matrix


def ensure_symmetric(
    matrix: np.ndarray,
    settings: Settings,
    rtol: float | None = None,
    atol: float | None = None,
) -> tuple[bool, float, float, np.ndarray]:
    """Check symmetry with a *relative* tolerance.

    The deviation is measured by the infinity norm ``d = ||A - A^T||_inf`` and
    compared against ``atol + rtol * max(1, ||A||_inf)`` so that scaled
    matrices are judged by relative error rather than absolute noise.

    Returns ``(is_symmetric, deviation, threshold, symmetrized)`` where
    ``symmetrized = (A + A^T)/2`` is always computed (used only after a
    successful check, as a best-practice removal of rounding-level skew).
    """
    rtol = settings.symmetry_rtol if rtol is None else rtol
    atol = settings.symmetry_atol if atol is None else atol
    skew = matrix - matrix.T
    deviation = float(np.max(np.abs(skew))) if skew.size else 0.0
    scale = max(1.0, float(np.max(np.abs(matrix))))
    threshold = atol + rtol * scale
    symmetrized = (matrix + matrix.T) * 0.5
    return deviation <= threshold, deviation, threshold, symmetrized
