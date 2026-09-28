"""Float-only diagnostic comparison (NumPy / SciPy).

Everything here is explicitly *inexact* and labelled as such.  It exists to
demonstrate the failure mode the exact service is designed to avoid: matrices
that are near-indistinguishable at binary-float precision get a wrong rank or
a spurious "solved" result.  Nothing in the authoritative solve/rank path may
call this module.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import numpy as np
from scipy import linalg as scipy_linalg


def to_float_matrix(rows: list[list[Fraction]]) -> np.ndarray:
    """Lossy conversion, used only for the diagnostic endpoint."""
    return np.array(
        [[float(v) for v in row] for row in rows], dtype=np.float64
    )


def float_diagnosis(
    A: list[list[Fraction]],
    b: list[Fraction] | None = None,
    *,
    exact_rank: int | None = None,
    tol: float | None = None,
) -> dict[str, Any]:
    """Compare NumPy/SciPy binary-float opinions against the known exact rank.

    Reports matrix_rank at the default tolerance and at several explicit
    tolerances, smallest singular values, and (for square A) a float ``solve``
    attempt with its residual.  Inputs are converted through ``float`` on
    purpose -- rounding flags are surfaced so the discrepancy is explainable.
    """
    m, n = len(A), len(A[0])
    Af = to_float_matrix(A)
    # Honest rounding check: converting the exact Fraction to float64 may
    # itself destroy information (e.g. 10**16 + 1 becomes 10**16).
    rounded_entries = any(
        Fraction(float(A[i][j])) != A[i][j]
        for i in range(m)
        for j in range(n)
    )

    singular_values = np.linalg.svd(Af, compute_uv=False)
    ranks_at_tols = {
        f"tol={t:.0e}": int(np.linalg.matrix_rank(Af, tol=t))
        for t in (1e-16, 1e-12, 1e-10, 1e-8)
    }
    diagnosis: dict[str, Any] = {
        "float_dtype": "float64",
        "input_entries_rounded_by_float": rounded_entries,
        "numpy_rank_default_tol": int(np.linalg.matrix_rank(Af, tol=tol)),
        "numpy_rank_at_explicit_tols": ranks_at_tols,
        "smallest_singular_values": [float(x) for x in singular_values[-3:]],
        "exact_rank": exact_rank,
    }
    if exact_rank is not None:
        diagnosis["rank_mismatch_vs_exact"] = (
            diagnosis["numpy_rank_default_tol"] != exact_rank
        )

    if b is not None and m == n:
        bf = np.array([float(v) for v in b], dtype=np.float64)
        solve_record: dict[str, Any] = {}
        try:
            x = np.linalg.solve(Af, bf)
            residual = Af @ x - bf
            solve_record.update(
                solved=True,
                float_solution=[float(v) for v in x],
                residual_inf_norm=float(np.max(np.abs(residual))),
            )
        except np.linalg.LinAlgError as exc:
            solve_record.update(solved=False, error=str(exc))
        # SciPy cross-check for completeness.
        try:
            lu, piv = scipy_linalg.lu_factor(Af)
            solve_record["scipy_lu_succeeded"] = True
        except ValueError as exc:  # singular
            solve_record.update(scipy_lu_succeeded=False, error=str(exc))
        diagnosis["float_solve"] = solve_record

    return diagnosis
