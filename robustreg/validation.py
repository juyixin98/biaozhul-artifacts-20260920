"""Input validation and numeric bounds for the JSON interface.

Everything is deliberately strict: silent coercion of malformed input is a
worse failure mode than a clear error for a small-library backend.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# ---------------------------------------------------------------------------
# Hard limits: keep problems "small to medium sized" and guard against
# pathological allocation / overflow.
# ---------------------------------------------------------------------------
MAX_N = 10_000          # maximum number of rows
MAX_P = 500             # maximum number of *user supplied* columns
MIN_N = 1
ABS_DATA_LIMIT = 1e100  # |x|, |y| bound (well below float64 overflow)
MAX_DELTA = 1e8
MAX_LAMBDA = 1e12
MAX_MAX_ITER = 10_000
MIN_RCOND = 1e-15       # tighter would be lost in roundoff

ALLOWED_TOP_LEVEL = {
    "X",
    "y",
    "method",
    "fit_intercept",
    "delta",
    "lam",
    "max_iter",
    "tol",
    "rcond",
    "allow_rank_deficient",
}


class RequestError(ValueError):
    """Raised for any malformed or out-of-range request payload."""


@dataclass(frozen=True)
class ParsedRequest:
    X: np.ndarray
    y: np.ndarray
    method: str
    fit_intercept: bool
    delta: float
    lam: float
    max_iter: int
    tol: float
    rcond: float | None
    allow_rank_deficient: bool


def _as_2d_float(name, value):
    """Convert ``value`` to a 2-D float64 ndarray; reject bool/non-numeric."""
    if isinstance(value, bool) or not isinstance(value, (list, tuple)):
        raise RequestError(
            f"field '{name}' must be a 2-D array (list of lists); got "
            f"{type(value).__name__}"
        )
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise RequestError(f"field '{name}' contains non-numeric entries: {exc}")
    if arr.ndim != 2:
        raise RequestError(
            f"field '{name}' must be 2-dimensional; got shape {arr.shape}"
        )
    if arr.size and arr.dtype.kind != "f":  # pragma: no cover - defensive
        raise RequestError(f"field '{name}' must contain real numbers")
    if not np.all(np.isfinite(arr)):
        raise RequestError(
            f"field '{name}' contains NaN or infinite values; all entries "
            "must be finite"
        )
    if np.any(np.abs(arr) > ABS_DATA_LIMIT):
        raise RequestError(
            f"field '{name}' has entries with |value| > {ABS_DATA_LIMIT:.0e}"
        )
    return arr


def _as_1d_float(name, value):
    if isinstance(value, bool) or not isinstance(value, (list, tuple)):
        raise RequestError(
            f"field '{name}' must be a 1-D array (list); got "
            f"{type(value).__name__}"
        )
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise RequestError(f"field '{name}' contains non-numeric entries: {exc}")
    if arr.ndim != 1:
        raise RequestError(
            f"field '{name}' must be 1-dimensional; got shape {arr.shape}"
        )
    if not np.all(np.isfinite(arr)):
        raise RequestError(
            f"field '{name}' contains NaN or infinite values; all entries "
            "must be finite"
        )
    if np.any(np.abs(arr) > ABS_DATA_LIMIT):
        raise RequestError(f"field '{name}' has |value| > {ABS_DATA_LIMIT:.0e}")
    return arr


def _scalar_bool(field, value, default):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise RequestError(f"field '{field}' must be true or false")
    return value


def _scalar_float(field, value, default, *, positive=False, nonneg=False,
                  maximum=None):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(f"field '{field}' must be a number")
    fv = float(value)
    if not math.isfinite(fv):
        raise RequestError(f"field '{field}' must be finite")
    if positive and fv <= 0.0:
        raise RequestError(f"field '{field}' must be > 0, got {fv}")
    if nonneg and fv < 0.0:
        raise RequestError(f"field '{field}' must be >= 0, got {fv}")
    if maximum is not None and fv > maximum:
        raise RequestError(f"field '{field}' must be <= {maximum}, got {fv}")
    return fv


def parse_request(request):
    """Validate a raw request dict into a :class:`ParsedRequest`.

    Raises :class:`RequestError` with a human-readable message on any problem.
    Unknown top-level fields are rejected so that typos cannot be silently
    ignored.
    """
    if not isinstance(request, dict):
        raise RequestError("request body must be a JSON object")

    unknown = set(request) - ALLOWED_TOP_LEVEL
    if unknown:
        raise RequestError(
            "unknown field(s): " + ", ".join(sorted(unknown)) + "; allowed: "
            + ", ".join(sorted(ALLOWED_TOP_LEVEL))
        )

    if "X" not in request:
        raise RequestError("missing required field 'X'")
    if "y" not in request:
        raise RequestError("missing required field 'y'")

    X = _as_2d_float("X", request["X"])
    y = _as_1d_float("y", request["y"])

    n, p_user = X.shape
    if n < MIN_N:
        raise RequestError("X must have at least one row")
    if n > MAX_N:
        raise RequestError(f"n={n} exceeds maximum {MAX_N} rows")
    if p_user == 0:
        raise RequestError("X must contain at least one column")
    if p_user > MAX_P:
        raise RequestError(
            f"p={p_user} (user columns) exceeds maximum {MAX_P}"
        )
    if y.shape[0] != n:
        raise RequestError(
            f"length of y ({y.shape[0]}) must match rows of X ({n})"
        )

    method = request.get("method", "huber")
    if method not in ("huber", "ols"):
        raise RequestError("field 'method' must be 'huber' or 'ols'")

    fit_intercept = _scalar_bool("fit_intercept", request.get("fit_intercept"),
                                 True)
    lam = _scalar_float("lam", request.get("lam"), 0.0, nonneg=True,
                        maximum=MAX_LAMBDA)
    delta = _scalar_float("delta", request.get("delta"), 1.345, positive=True,
                          maximum=MAX_DELTA)
    max_iter = int(
        _scalar_float("max_iter", request.get("max_iter"), 50, positive=True,
                      maximum=MAX_MAX_ITER)
    )
    tol = _scalar_float("tol", request.get("tol"), 1e-8, positive=True,
                        maximum=1.0)
    if "rcond" in request and request["rcond"] is not None:
        rcond = _scalar_float("rcond", request["rcond"], None, positive=True)
        if rcond < MIN_RCOND:
            raise RequestError(
                f"field 'rcond' must be >= {MIN_RCOND} (below machine "
                "roundoff threshold)"
            )
    else:
        rcond = None
    allow_rank_deficient = _scalar_bool(
        "allow_rank_deficient", request.get("allow_rank_deficient"), False
    )

    p_design = p_user + (1 if fit_intercept else 0)
    if p_design > MAX_P:
        raise RequestError(
            f"design matrix has {p_design} columns (including intercept), "
            f"exceeds maximum {MAX_P}"
        )

    return ParsedRequest(
        X=X,
        y=y,
        method=method,
        fit_intercept=fit_intercept,
        delta=delta,
        lam=lam,
        max_iter=max_iter,
        tol=tol,
        rcond=rcond,
        allow_rank_deficient=allow_rank_deficient,
    )
