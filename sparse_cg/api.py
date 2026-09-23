"""JSON request/response interface for the sparse-CG backend.

The API layer validates a JSON payload (already parsed into Python objects),
builds the :class:`CSRMatrix`, runs preconditioned CG and returns a
JSON-serializable response dict.  It never raises for *valid requests with
unsuccessful solves* -- those come back with an error status inside the
normal response envelope.  Malformed requests raise
:class:`InvalidRequestError`, which carries a stable machine-readable
``code``.

Request schema
--------------
``matrix`` (required) describes A in CSR form:

* ``n``           int, 1 <= n <= 10_000
* ``indptr``      int array of length n+1
* ``indices``     int array, every entry in [0, n)
* ``data``        number array, same length as ``indices``
* (COO alternative: ``rows``/``cols``/``values`` triplets)

``b`` (required)     number array of length n.
``x0`` (optional)    number array of length n (default: zero vector).

Optional solver fields:

* ``tol``                    number in [1e-14, 1e-2]        (default 1e-8)
* ``max_iter``               int in [1, 100_000] or null    (default 10*n)
* ``preconditioner``         "none" | "jacobi"              (default "none")
* ``true_residual_every``    int >= 0                       (default 1)
* ``stall_window``           int >= 1                       (default 50)
* ``stall_tolerance``        number in (0, 1)               (default 0.8)
* ``symmetry_tol``           number in (0, 1]               (default 1e-10)
* ``symmetry_atol``          number >= 0                    (default 1e-12)
* ``assume_spd``             bool, true = skip symmetry &
                             positive-diagonal checks       (default false)
* ``include_history``        bool                           (default true)
* ``include_solution``       bool                           (default true)

Response envelope (HTTP-ish, but usable on stdin/stdout)
--------------------------------------------------------
``{"ok": true,  "result": {...},   "diagnostics": {...}}`` on a successful
solve (check ``result.converged``), or
``{"ok": false, "error": {"code", "message"}}`` for a rejected request.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .csr import CSRMatrix
from .exceptions import (
    InvalidCsrError,
    InvalidRequestError,
    NonSymmetricMatrixError,
    NotPositiveDefiniteError,
    SparseCgError,
    ValidationError,
)
from .limits import (
    MAX_ABS_VALUE,
    MAX_ITER_CAP,
    MAX_N,
    MAX_NNZ,
    MAX_TOL,
    MIN_TOL,
)
from .solver import PCGConfig, solve_pcg

# Every top-level key we understand.  Unknown keys are rejected on purpose:
# silently ignoring a misspelled field ("tolerance" vs "tol") is a classic
# source of confusing results.
_KNOWN_KEYS = frozenset({
    "matrix", "b", "x0", "tol", "max_iter", "preconditioner",
    "true_residual_every", "stall_window", "stall_tolerance",
    "symmetry_tol", "symmetry_atol", "assume_spd",
    "include_history", "include_solution",
})
_KNOWN_MATRIX_KEYS = frozenset({
    "n", "indptr", "indices", "data", "rows", "cols", "values",
})


def _err(code: str, message: str, *, details: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"ok": False, "error": error}


def _is_real_number(v: Any) -> bool:
    """JSON numbers only: int/float, excluding bool and non-finite floats."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, float):
        return math.isfinite(v)
    return False


def _as_number(v: Any, name: str) -> float:
    if not _is_real_number(v):
        raise InvalidRequestError(
            f"field {name!r} must be a finite JSON number, got "
            f"{v!r} ({type(v).__name__})"
        )
    return float(v)


def _as_int(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise InvalidRequestError(
            f"field {name!r} must be a JSON integer, got {v!r} "
            f"({type(v).__name__})"
        )
    return int(v)


def _as_number_array(v: Any, name: str, *, length: int | None = None
                     ) -> np.ndarray:
    if not isinstance(v, list):
        raise InvalidRequestError(
            f"field {name!r} must be a JSON array, got {type(v).__name__}"
        )
    if length is not None and len(v) != length:
        raise InvalidRequestError(
            f"field {name!r} must have length {length}, got {len(v)}"
        )
    out = np.empty(len(v), dtype=np.float64)
    for i, item in enumerate(v):
        if not _is_real_number(item):
            raise InvalidRequestError(
                f"field {name!r}[{i}] must be a finite JSON number, got "
                f"{item!r} ({type(item).__name__})"
            )
        out[i] = float(item)
        if abs(out[i]) > MAX_ABS_VALUE:
            raise InvalidRequestError(
                f"field {name!r}[{i}]={out[i]:.3e} exceeds MAX_ABS_VALUE "
                f"({MAX_ABS_VALUE:.0e})"
            )
    return out


def _as_int_array(v: Any, name: str) -> np.ndarray:
    if not isinstance(v, list):
        raise InvalidRequestError(
            f"field {name!r} must be a JSON array, got {type(v).__name__}"
        )
    out = np.empty(len(v), dtype=np.int64)
    for i, item in enumerate(v):
        if isinstance(item, bool) or not isinstance(item, int):
            raise InvalidRequestError(
                f"field {name!r}[{i}] must be a JSON integer, got "
                f"{item!r} ({type(item).__name__})"
            )
        out[i] = int(item)
    return out


def _build_matrix(mat: Any) -> tuple[CSRMatrix, int, int]:
    """Parse the 'matrix' object; returns (CSRMatrix, n, declared_nnz)."""
    if not isinstance(mat, dict):
        raise InvalidRequestError(
            f"'matrix' must be an object, got {type(mat).__name__}"
        )
    unknown = set(mat) - _KNOWN_MATRIX_KEYS
    if unknown:
        raise InvalidRequestError(
            f"unknown matrix field(s): {sorted(unknown)}; allowed: "
            f"{sorted(_KNOWN_MATRIX_KEYS)}"
        )
    if "n" not in mat:
        raise InvalidRequestError("matrix.n is required")
    n = _as_int(mat["n"], "matrix.n")
    if not (1 <= n <= MAX_N):
        raise InvalidRequestError(
            f"matrix.n must satisfy 1 <= n <= {MAX_N}, got {n}"
        )

    has_csr = all(k in mat for k in ("indptr", "indices", "data"))
    has_any_csr = any(k in mat for k in ("indptr", "indices", "data"))
    has_coo = all(k in mat for k in ("rows", "cols", "values"))
    has_any_coo = any(k in mat for k in ("rows", "cols", "values"))

    if has_csr and has_coo:
        raise InvalidRequestError(
            "matrix must use either CSR (indptr/indices/data) or COO "
            "(rows/cols/values), not both"
        )
    if has_any_csr and not has_csr:
        missing = [k for k in ("indptr", "indices", "data") if k not in mat]
        raise InvalidRequestError(
            f"CSR matrix is missing field(s): {missing}"
        )
    if has_any_coo and not has_coo:
        missing = [k for k in ("rows", "cols", "values") if k not in mat]
        raise InvalidRequestError(
            f"COO matrix is missing field(s): {missing}"
        )
    if not has_csr and not has_coo:
        raise InvalidRequestError(
            "matrix must contain CSR fields (indptr, indices, data) or COO "
            "fields (rows, cols, values)"
        )

    if has_csr:
        indptr = _as_int_array(mat["indptr"], "matrix.indptr")
        indices = _as_int_array(mat["indices"], "matrix.indices")
        data = _as_number_array(mat["data"], "matrix.data")
        declared_nnz = int(data.size)
        # InvalidCsrError propagates unchanged (code "invalid_csr"); it is a
        # ValidationError and is converted to the error envelope by
        # handle_request.
        A = CSRMatrix(n, indptr, indices, data)
    else:
        rows = _as_int_array(mat["rows"], "matrix.rows")
        cols = _as_int_array(mat["cols"], "matrix.cols")
        values = _as_number_array(mat["values"], "matrix.values")
        declared_nnz = int(values.size)
        A = CSRMatrix.from_triplets(n, rows, cols, values)

    if A.nnz > MAX_NNZ:
        raise InvalidRequestError(
            f"matrix nnz={A.nnz} exceeds MAX_NNZ={MAX_NNZ}"
        )
    return A, n, declared_nnz


def _optional_bool(payload: dict[str, Any], name: str, default: bool) -> bool:
    if name not in payload:
        return default
    v = payload[name]
    if not isinstance(v, bool):
        raise InvalidRequestError(f"field {name!r} must be true or false")
    return v


def process_request(payload: Any) -> dict[str, Any]:
    """Validate a parsed JSON request and execute the solve.

    Returns the response envelope (always a plain JSON-serializable dict).
    Raises :class:`InvalidRequestError` only for payloads malformed enough
    that no sensible envelope can be built -- call :func:`handle_request`
    to get those converted too.
    """
    if not isinstance(payload, dict):
        raise InvalidRequestError(
            f"request body must be a JSON object, got {type(payload).__name__}"
        )
    unknown = set(payload) - _KNOWN_KEYS
    if unknown:
        raise InvalidRequestError(
            f"unknown top-level field(s): {sorted(unknown)}; allowed: "
            f"{sorted(_KNOWN_KEYS)}"
        )
    if "matrix" not in payload:
        raise InvalidRequestError("missing required field 'matrix'")
    if "b" not in payload:
        raise InvalidRequestError("missing required field 'b'")

    A, n, declared_nnz = _build_matrix(payload["matrix"])
    b = _as_number_array(payload["b"], "b", length=n)
    x0 = None
    if "x0" in payload:
        x0 = _as_number_array(payload["x0"], "x0", length=n)

    # ---- solver options with explicit range checks ------------------------
    kwargs: dict[str, Any] = {}

    if "tol" in payload:
        tol = _as_number(payload["tol"], "tol")
        if not (MIN_TOL <= tol <= MAX_TOL):
            raise InvalidRequestError(
                f"'tol' must lie in [{MIN_TOL:g}, {MAX_TOL:g}], got {tol:g}"
            )
        kwargs["tol"] = tol

    if "max_iter" in payload:
        mi = payload["max_iter"]
        if mi is None:
            kwargs["max_iter"] = None
        else:
            mi = _as_int(mi, "max_iter")
            if not (1 <= mi <= MAX_ITER_CAP):
                raise InvalidRequestError(
                    f"'max_iter' must lie in [1, {MAX_ITER_CAP}], got {mi}"
                )
            kwargs["max_iter"] = mi

    if "preconditioner" in payload:
        pc = payload["preconditioner"]
        if pc not in ("none", "jacobi"):
            raise InvalidRequestError(
                f"'preconditioner' must be 'none' or 'jacobi', got {pc!r}"
            )
        kwargs["preconditioner"] = pc

    for name in ("true_residual_every", "stall_window"):
        if name in payload:
            val = _as_int(payload[name], name)
            if val < (0 if name == "true_residual_every" else 1):
                raise InvalidRequestError(
                    f"'{name}' must be >= "
                    f"{0 if name == 'true_residual_every' else 1}, got {val}"
                )
            kwargs[name] = val

    if "stall_tolerance" in payload:
        st = _as_number(payload["stall_tolerance"], "stall_tolerance")
        if not (0.0 < st < 1.0):
            raise InvalidRequestError(
                f"'stall_tolerance' must lie in (0, 1), got {st:g}"
            )
        kwargs["stall_tolerance"] = st

    sym_rtol = 1e-10
    sym_atol = 1e-12
    if "symmetry_tol" in payload:
        sym_rtol = _as_number(payload["symmetry_tol"], "symmetry_tol")
        if not (0.0 < sym_rtol <= 1.0):
            raise InvalidRequestError(
                f"'symmetry_tol' must lie in (0, 1], got {sym_rtol:g}"
            )
    if "symmetry_atol" in payload:
        sym_atol = _as_number(payload["symmetry_atol"], "symmetry_atol")
        if sym_atol < 0.0:
            raise InvalidRequestError(
                f"'symmetry_atol' must be >= 0, got {sym_atol:g}"
            )

    assume_spd = _optional_bool(payload, "assume_spd", False)
    include_history = _optional_bool(payload, "include_history", True)
    include_solution = _optional_bool(payload, "include_solution", True)

    # ---- SPD checks (unless the caller explicitly assumes them) ------------
    # Structural CSR failures were converted to InvalidRequestError while
    # building the matrix; symmetry/definiteness errors propagate with their
    # own stable codes (non_symmetric_matrix / not_positive_definite).
    symmetry_diff: float | None = None
    if not assume_spd:
        A.assert_symmetric(rtol=sym_rtol, atol=sym_atol)
        if kwargs.get("preconditioner", "none") != "jacobi":
            # Jacobi path: solve_pcg itself checks the positive diagonal.
            A.assert_positive_diagonal()
        # Compute the worst symmetry diff for diagnostics (the check just
        # verified symmetry, so the mirrored entry always exists).
        symmetry_diff = _max_symmetry_diff(A)

    # ---- solve --------------------------------------------------------------
    cfg = PCGConfig(**kwargs)
    result = solve_pcg(A, b, x0=x0, config=cfg)

    result_dict = result.to_dict()
    if not include_history:
        result_dict.pop("history")
    if not include_solution:
        result_dict.pop("x")

    diagnostics = {
        "n": int(n),
        "nnz_stored": int(A.nnz),
        "nnz_declared": int(declared_nnz),
        "duplicates_or_zeros_dropped": int(declared_nnz - A.nnz),
        "symmetry_max_abs_diff": (
            None if symmetry_diff is None else float(symmetry_diff)
        ),
        "tol": cfg.tol,
        "max_iter_used": int(cfg.max_iter if cfg.max_iter is not None
                             else min(10 * n, MAX_ITER_CAP)),
        "residual_verified_true": True,
    }

    return {"ok": True, "result": result_dict, "diagnostics": diagnostics}


def _max_symmetry_diff(A: CSRMatrix) -> float:
    """Worst |A_ij - A_ji| over stored entries (assumes symmetric pattern)."""
    worst = 0.0
    for i in range(A.n):
        s, e = A.indptr[i], A.indptr[i + 1]
        for k in range(e - s):
            j = int(A.indices[s + k])
            ts, te = A.indptr[j], A.indptr[j + 1]
            loc = np.searchsorted(A.indices[ts:te], i)
            if ts + loc < te and A.indices[ts + loc] == i:
                worst = max(
                    worst, abs(float(A.data[s + k]) - float(A.data[ts + loc]))
                )
    return worst


def handle_request(payload: Any) -> dict[str, Any]:
    """Like :func:`process_request`, but never raises.

    Converts every :class:`SparseCgError` into the ``{"ok": false, ...}``
    error envelope, and also guards against unexpected library errors so the
    CLI can always emit valid JSON.
    """
    try:
        return process_request(payload)
    except ValidationError as exc:
        details: dict[str, Any] = {}
        if isinstance(exc, NonSymmetricMatrixError):
            details["max_abs_diff"] = exc.max_abs_diff
            if exc.position is not None:
                details["position"] = list(exc.position)
        elif isinstance(exc, NotPositiveDefiniteError):
            if exc.index >= 0:
                details["index"] = exc.index
            if exc.diagonal_value is not None:
                details["diagonal_value"] = exc.diagonal_value
        return _err(exc.code, str(exc), details=details or None)
    except SparseCgError as exc:  # defensive: unknown library error
        return _err("sparse_cg_error", str(exc))
