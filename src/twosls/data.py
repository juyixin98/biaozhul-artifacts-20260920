"""Boundary validation: request payload -> validated EstimationData.

All untrusted input crosses this module. Checks performed:

* column-name references resolve and role lists are disjoint
* observation counts agree and respect configured size limits
* values are finite floats
* an intercept is appended (or an existing ``const`` column reused)
* there are enough degrees of freedom for the requested covariance
"""
from __future__ import annotations

import numpy as np

from .config import settings
from .contract import EstimationData, EstimationRequest
from .errors import ValidationError

INTERCEPT_NAME = "const"


def _require_finite(rid: str, **arrays: np.ndarray) -> None:
    """Boundary check: non-finite input cells are a validation failure."""
    for name, arr in arrays.items():
        if arr.size and not np.all(np.isfinite(arr)):
            bad = int((~np.isfinite(arr)).sum())
            raise ValidationError(
                f"matrix {name!r} contains {bad} non-finite cell(s)",
                request_id=rid,
                key_state={"matrix": name, "non_finite_cells": bad},
            )


def build_data(request: EstimationRequest) -> EstimationData:
    rid = request.request_id
    spec = request.spec
    cols = request.columns

    n = len(next(iter(cols.values())))

    # ---- size guards -----------------------------------------------------
    if n < settings.min_observations:
        raise ValidationError(
            f"n={n} below minimum {settings.min_observations}",
            request_id=rid,
            key_state={"nobs": n, "min": settings.min_observations},
        )
    if n > settings.max_observations:
        raise ValidationError(
            f"n={n} above maximum {settings.max_observations}",
            request_id=rid,
            key_state={"nobs": n, "max": settings.max_observations},
        )

    referenced = [spec.dependent, *spec.endogenous, *spec.included_exogenous, *spec.excluded_instruments]
    missing = sorted({name for name in referenced if name not in cols})
    if missing:
        raise ValidationError(
            f"referenced columns absent from payload: {missing}",
            request_id=rid,
            key_state={"missing": missing},
        )

    # role lists must be pairwise disjoint
    seen: dict[str, str] = {}
    for role, names in (
        ("dependent", [spec.dependent]),
        ("endogenous", spec.endogenous),
        ("included_exogenous", spec.included_exogenous),
        ("excluded_instruments", spec.excluded_instruments),
    ):
        for name in names:
            if name in seen:
                raise ValidationError(
                    f"column {name!r} assigned to both {seen[name]!r} and {role!r}",
                    request_id=rid,
                    key_state={"column": name, "roles": [seen[name], role]},
                )
            seen[name] = role

    total_cols = 1 + len(spec.endogenous) + len(spec.included_exogenous) + len(spec.excluded_instruments)
    if total_cols > settings.max_columns:
        raise ValidationError(
            f"total design columns {total_cols} exceed limit {settings.max_columns}",
            request_id=rid,
            key_state={"columns": total_cols, "limit": settings.max_columns},
        )

    # ---- materialize -----------------------------------------------------
    y = np.asarray(cols[spec.dependent], dtype=float)

    def stack(names: list[str]) -> np.ndarray:
        if not names:
            return np.empty((n, 0))
        return np.column_stack([np.asarray(cols[name], dtype=float) for name in names])

    Y = stack(spec.endogenous)
    X_named = stack(spec.included_exogenous)
    Z = stack(spec.excluded_instruments)

    for label, mat in (("y", y), ("Y", Y), ("X", X_named), ("Z", Z)):
        _require_finite(rid, **{label: mat})

    exog_names = list(spec.included_exogenous)

    # ---- intercept handling ---------------------------------------------
    if request.options.add_intercept and INTERCEPT_NAME not in exog_names:
        if INTERCEPT_NAME in cols and INTERCEPT_NAME not in seen:
            # A literal constant column supplied but not assigned: adopt it.
            X = np.column_stack([X_named, np.asarray(cols[INTERCEPT_NAME], dtype=float)])
        else:
            X = np.column_stack([X_named, np.ones(n)]) if X_named.size else np.ones((n, 1))
        exog_names = exog_names + [INTERCEPT_NAME]
    else:
        X = X_named

    # constant-variance / zero-variance columns (other than the intercept)
    for name, mat in (
        ("Y", Y),
        ("X", X),
        ("Z", Z),
    ):
        if mat.size:
            spans = mat.max(axis=0) - mat.min(axis=0)
            zero_var = int(np.sum(spans <= 1e-12 * (1.0 + np.abs(mat).max())))
            if zero_var and name != "X":
                raise ValidationError(
                    f"{zero_var} zero-variance column(s) in {name}",
                    request_id=rid,
                    key_state={"block": name, "zero_variance_columns": zero_var},
                )

    # ---- residual degrees of freedom ------------------------------------
    k, m, L = Y.shape[1], X.shape[1], Z.shape[1]
    df_resid = n - (k + m)
    if df_resid <= 0:
        raise ValidationError(
            f"no residual degrees of freedom: n={n} against {k + m} structural regressors",
            request_id=rid,
            key_state={"nobs": n, "regressors": k + m, "df_resid": df_resid},
        )
    if n - (m + L) <= 0:
        raise ValidationError(
            f"no degrees of freedom for first stage: n={n} against {m + L} instruments+controls",
            request_id=rid,
            key_state={"nobs": n, "first_stage_regressors": m + L},
        )

    return EstimationData(
        request_id=rid,
        y=y,
        Y=Y,
        X=X,
        Z=Z,
        endog_names=tuple(spec.endogenous),
        exog_names=tuple(exog_names),
        instrument_names=tuple(spec.excluded_instruments),
    )
