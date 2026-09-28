"""Strict input validation at the system boundary.

Fails fast with :class:`ValidationError` and a stable ``details`` payload.
Nothing downstream trusts external arrays.
"""

from __future__ import annotations

import numpy as np

from .errors import ValidationError


def validate_xy(
    x: np.ndarray | list[list[float]],
    a: np.ndarray | list[float],
    y: np.ndarray | list[float],
    *,
    max_observations: int,
    max_covariates: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Validate and coerce covariates X, treatment A, outcome Y.

    Returns contiguous float64 / bool arrays. Raises ValidationError with a
    machine-readable reason on every rejection.
    """
    try:
        x_arr = np.asarray(x, dtype=np.float64)
        a_arr = np.asarray(a, dtype=np.float64)
        y_arr = np.asarray(y, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Inputs must be numeric arrays: {exc}") from exc

    if x_arr.ndim != 2:
        raise ValidationError(
            "X must be a 2-D matrix (n_observations x n_covariates)",
            details={"x_ndim": x_arr.ndim},
        )
    n, p = x_arr.shape
    if n < 10:
        raise ValidationError(
            "Need at least 10 observations for cross-fitted estimation", details={"n": n}
        )
    if n > max_observations:
        raise ValidationError(
            "Too many observations for this service",
            details={"n": n, "limit": max_observations},
        )
    if p == 0:
        raise ValidationError("X must contain at least one covariate")
    if p > max_covariates:
        raise ValidationError(
            "Too many covariates for this service",
            details={"p": p, "limit": max_covariates},
        )
    if a_arr.shape != (n,):
        raise ValidationError(
            "A must have shape (n,)", details={"a_shape": list(a_arr.shape), "n": n}
        )
    if y_arr.shape != (n,):
        raise ValidationError(
            "Y must have shape (n,)", details={"y_shape": list(y_arr.shape), "n": n}
        )

    if not np.all(np.isfinite(x_arr)):
        raise ValidationError(
            "X contains NaN or infinite values; imputation is not silently applied",
            details={"n_nonfinite": int(np.sum(~np.isfinite(x_arr)))},
        )
    if not np.all(np.isfinite(y_arr)):
        raise ValidationError(
            "Y contains NaN or infinite values",
            details={"n_nonfinite": int(np.sum(~np.isfinite(y_arr)))},
        )

    unique_a = np.unique(a_arr[np.isfinite(a_arr)])
    non_binary = [float(v) for v in unique_a if v not in (0.0, 1.0)]
    if non_binary or not np.all(np.isfinite(a_arr)):
        raise ValidationError(
            "Treatment A must be binary {0,1}; other values are not recoded",
            details={"example_bad_values": non_binary[:5]},
        )

    n_treated = int(a_arr.sum())
    n_untreated = n - n_treated
    if n_treated < 2 or n_untreated < 2:
        raise ValidationError(
            "Both treatment groups need at least 2 units",
            details={"n_treated": n_treated, "n_untreated": n_untreated},
        )

    return x_arr, a_arr.astype(np.int8), y_arr


def validate_fold_sizes(a: np.ndarray, n_splits: int) -> None:
    """Ensure each class can populate every fold with both labels in train/eval."""
    n_treated = int(a.sum())
    n_untreated = int(len(a) - n_treated)
    if n_splits > min(n_treated, n_untreated):
        raise ValidationError(
            "n_splits exceeds the smallest treatment group; folds would be empty",
            details={
                "n_splits": n_splits,
                "n_treated": n_treated,
                "n_untreated": n_untreated,
            },
        )
