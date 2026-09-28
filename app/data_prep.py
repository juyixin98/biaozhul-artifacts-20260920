"""System-boundary validation: request payload -> validated numpy matrices."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .contracts import EstimateRequest
from .errors import (
    IncompatibleShapes,
    InsufficientObservations,
    NonFiniteData,
)

_GROUPS = ("dependent", "endogenous", "exogenous", "instruments")


@dataclass(frozen=True)
class PreparedData:
    y: np.ndarray
    x: np.ndarray                    # endogenous regressors (n, K)
    w: np.ndarray                    # included exogenous incl. constant (n, J)
    z: np.ndarray                    # excluded instruments (n, L)
    names_endog: tuple[str, ...]
    names_exog: tuple[str, ...]
    names_instruments: tuple[str, ...]
    n_obs: int


def _column(payload: EstimateRequest, name: str) -> np.ndarray:
    try:
        return np.asarray(payload.columns[name], dtype=np.float64)
    except KeyError as exc:
        raise IncompatibleShapes(
            f"column '{name}' referenced but not present in payload",
            details={"missing_column": name},
        ) from exc


def _check_unique_roles(req: EstimateRequest) -> None:
    seen: dict[str, str] = {}
    groups = {
        "dependent": [req.dependent],
        "endogenous": req.endogenous,
        "exogenous": req.exogenous,
        "instruments": req.instruments,
    }
    for group, names in groups.items():
        for name in names:
            previous = seen.get(name)
            if previous is not None:
                raise IncompatibleShapes(
                    f"column '{name}' appears in both '{previous}' and '{group}'",
                    details={"column": name, "groups": [previous, group]},
                )
            seen[name] = group


def _build_matrix(req: EstimateRequest, names: list[str], n: int) -> np.ndarray:
    if not names:
        return np.empty((n, 0))
    cols = []
    for name in names:
        col = _column(req, name)
        if col.ndim != 1 or col.shape[0] != n:
            raise IncompatibleShapes(
                f"column '{name}' length {col.shape[0]} != n={n}",
                details={"column": name, "expected_length": n,
                         "actual_length": int(col.shape[0])},
            )
        cols.append(col)
    return np.column_stack(cols)


def _check_finite(label: str, mat: np.ndarray) -> None:
    if not np.all(np.isfinite(mat)):
        raise NonFiniteData(
            f"non-finite values (NaN/Inf) in {label} columns",
            details={"group": label},
        )


def prepare_data(req: EstimateRequest, *, max_obs: int) -> PreparedData:
    _check_unique_roles(req)

    y = _column(req, req.dependent)
    if y.ndim != 1:
        raise IncompatibleShapes("dependent column must be one-dimensional")
    n = y.shape[0]
    if n > max_obs:
        raise IncompatibleShapes(
            f"n={n} exceeds configured maximum {max_obs}",
            details={"n_obs": n, "max_obs": max_obs},
        )

    x = _build_matrix(req, req.endogenous, n)
    w_user = _build_matrix(req, req.exogenous, n)
    z = _build_matrix(req, req.instruments, n)

    for label, mat in (("dependent", y), ("endogenous", x),
                       ("exogenous", w_user), ("instruments", z)):
        _check_finite(label, mat)

    w = np.column_stack([np.ones(n), w_user]) if req.add_constant else w_user
    min_n = w.shape[1] + x.shape[1] + 1
    if n < min_n:
        raise InsufficientObservations(
            f"n={n} too small: need at least {min_n} observations for this "
            "specification (regressors plus residual df)",
            details={"n_obs": n, "minimum_required": min_n},
        )

    return PreparedData(
        y=y, x=x, w=w, z=z,
        names_endog=tuple(req.endogenous),
        names_exog=tuple(req.exogenous),
        names_instruments=tuple(req.instruments),
        n_obs=n,
    )
