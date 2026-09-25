"""Huber regression via Iteratively Reweighted Least Squares (IRLS).

Huber loss for residual r with transition scale delta::

    rho(r) = 0.5 r^2                      if |r| <= delta
             delta (|r| - 0.5 delta)      if |r| >  delta

It is quadratic near zero (efficient for Gaussian noise) and linear in the
tails (bounded influence of outliers). IRLS exploits the identity
``grad rho(r) = psi(r) = w(r) r`` with
``w(r) = 1`` for |r| <= delta and ``w(r) = delta / |r|`` otherwise, so each
outer iteration is a weighted least squares problem

    b^{(k+1)} = argmin_b  sum_i w(r_i^{(k)}) (y_i - A_i b)^2
                        + sum_j pen_j b_j^2

The intercept column (added when ``fit_intercept=True``) carries zero penalty.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np

from robustreg.wls import RankDeficientError, weighted_ridge_solve

# Objective is allowed to "increase" across an IRLS step only by this much
# relative to the initial objective scale (floating-point slack).
OBJECTIVE_SLACK = 1e-10


@dataclass
class HuberResult:
    """Outcome of :func:`huber_irls`."""

    coef: np.ndarray              # slopes, shape (p_user,)
    intercept: float              # 0.0 when fit_intercept is False
    residual: np.ndarray          # y - A @ beta, shape (n,)
    weights: np.ndarray           # final IRLS weights, shape (n,)
    objective: float              # final sum of Huber loss (no penalty term)
    penalized_objective: float    # objective including 0.5 * lam * ||coef||^2
    iterations: int               # weighted least squares solves performed
    converged: bool               # coefficient-change criterion met
    objective_decreased: bool     # monotone objective across iterations
    max_iter: int
    tol: float
    delta: float
    rank: int
    rank_deficient: bool
    nullspace_dim: int
    smallest_singular_value: float
    singular_value_threshold: float
    singular_values: np.ndarray
    objective_history: list = field(default_factory=list)
    coef_norm_history: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return {
            "coef": [float(v) for v in self.coef],
            "intercept": float(self.intercept),
            "residual": [float(v) for v in self.residual],
            "weights": [float(v) for v in self.weights],
            "objective": float(self.objective),
            "penalized_objective": float(self.penalized_objective),
            "iterations": int(self.iterations),
            "converged": bool(self.converged),
            "objective_decreased": bool(self.objective_decreased),
            "max_iter": int(self.max_iter),
            "tol": float(self.tol),
            "delta": float(self.delta),
            "rank": int(self.rank),
            "rank_deficient": bool(self.rank_deficient),
            "nullspace_dim": int(self.nullspace_dim),
            "smallest_singular_value": float(self.smallest_singular_value),
            "singular_value_threshold": float(self.singular_value_threshold),
            "singular_values": [float(v) for v in self.singular_values],
            "objective_history": [float(v) for v in self.objective_history],
            "coef_norm_history": [float(v) for v in self.coef_norm_history],
            "warnings": list(self.warnings),
        }


@dataclass
class OLSResult:
    """Outcome of :func:`ols_fit` (the non-robust baseline)."""

    coef: np.ndarray
    intercept: float
    residual: np.ndarray
    sse: float                    # sum of squared errors (0.5-weighted sum)
    huber_objective: float        # Huber loss evaluated at the OLS residuals
    rank: int
    rank_deficient: bool
    nullspace_dim: int
    smallest_singular_value: float
    singular_value_threshold: float
    singular_values: np.ndarray
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return {
            "coef": [float(v) for v in self.coef],
            "intercept": float(self.intercept),
            "residual": [float(v) for v in self.residual],
            "sse": float(self.sse),
            "huber_objective": float(self.huber_objective),
            "rank": int(self.rank),
            "rank_deficient": bool(self.rank_deficient),
            "nullspace_dim": int(self.nullspace_dim),
            "smallest_singular_value": float(self.smallest_singular_value),
            "singular_value_threshold": float(self.singular_value_threshold),
            "singular_values": [float(v) for v in self.singular_values],
            "warnings": list(self.warnings),
        }


def huber_loss(r, delta):
    """Elementwise Huber rho (see module docstring)."""
    r = np.abs(r)
    quad = 0.5 * r * r
    lin = delta * (r - 0.5 * delta)
    return np.where(r <= delta, quad, lin)


def huber_objective(r, delta):
    """Sum of Huber losses over residuals ``r``."""
    return float(np.sum(huber_loss(r, delta)))


def huber_weight(r, delta):
    """IRLS weight: 1 in the quadratic region, delta/|r| in the tails.

    Exact zero residuals get weight 1 (psi(r)/r -> 1 as r -> 0).
    """
    w = np.ones_like(r, dtype=np.float64)
    absr = np.abs(r)
    tail = absr > delta
    # max(|r|, delta) avoids division by zero even if a non-finite/very small
    # residual ever reaches this function; tail rows have |r| > delta > 0.
    w[tail] = delta / np.maximum(absr[tail], delta)
    return w


def _build_design(X, fit_intercept):
    """Augment X with a leading all-ones column when requested."""
    if fit_intercept:
        n = X.shape[0]
        return np.hstack([np.ones((n, 1), dtype=np.float64), X])
    return X


def _penalty_vector(p_design, fit_intercept, lam):
    pen = np.full(p_design, float(lam), dtype=np.float64)
    if fit_intercept:
        pen[0] = 0.0  # intercept is never penalized
    return pen


def _solve(A, y, w, pen, rcond, allow_rank_deficient):
    """Thin wrapper around the SVD solver returning the info we report."""
    sol = weighted_ridge_solve(
        A, y, w=w, pen=pen, rcond=rcond,
        allow_rank_deficient=allow_rank_deficient,
    )
    info = {
        "rank": sol["rank"],
        "rank_deficient": sol["rank_deficient"],
        "singular_values": sol["singular_values"],
        "threshold": sol["threshold"],
        "nullspace_dim": A.shape[1] - sol["rank"],
        "smallest_sv": (
            float(sol["singular_values"][sol["rank"] - 1])
            if sol["rank"] > 0 else 0.0
        ),
    }
    return sol["beta"], info


def huber_irls(X, y, *, delta=1.345, fit_intercept=True, lam=0.0,
               max_iter=50, tol=1e-8, rcond=None,
               allow_rank_deficient=False):
    """Fit Huber regression by IRLS.

    Parameters
    ----------
    X:
        Feature matrix (n, p).
    y:
        Response (n,).
    delta:
        Huber transition scale; must be > 0. Smaller = more robust but less
        efficient. 1.345 gives 95% efficiency under Gaussian noise.
    fit_intercept:
        If True a constant column is prepended and never penalized.
    lam:
        L2 (ridge) penalty on the *slope* coefficients only; >= 0.
    max_iter:
        Maximum number of weighted least squares solves (>= 1).
    tol:
        Convergence tolerance: stop when
        ``max|b_new - b_old| <= tol * max(1, max|b_new|)``.
    rcond:
        Relative singular-value cutoff for the rank decision (see
        :func:`weighted_ridge_solve`).
    allow_rank_deficient:
        If False (default), a rank-deficient design raises
        :class:`~robustreg.wls.RankDeficientError` immediately. If True the SVD
        minimum-norm solution is used and a warning is recorded.

    Returns
    -------
    HuberResult
    """
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if delta <= 0.0:
        raise ValueError("delta must be > 0")
    if lam < 0.0:
        raise ValueError("lam must be >= 0")
    if max_iter < 1:
        raise ValueError("max_iter must be >= 1")
    if tol <= 0.0:
        raise ValueError("tol must be > 0")

    A = _build_design(X, fit_intercept)
    n, p_design = A.shape
    pen = _penalty_vector(p_design, fit_intercept, lam)

    warns = []
    # Initial fit: ordinary (equal-weight) penalized least squares.
    beta, info = _solve(A, y, None, pen, rcond, allow_rank_deficient)
    if info["rank_deficient"]:
        msg = (
            f"design matrix is rank deficient (rank {info['rank']}/"
            f"{p_design}); using SVD minimum-norm solution"
        )
        warns.append(msg)
        warnings.warn(msg, RuntimeWarning, stacklevel=2)

    residual = y - A @ beta
    obj = huber_objective(residual, delta)
    history = [obj]
    beta_norm_hist = [float(np.max(np.abs(beta)))]
    monotone = True
    converged = False
    iterations = 1
    prev_beta = beta
    prev_obj = obj

    for k in range(1, max_iter):
        w = huber_weight(residual, delta)
        # Deficiency can appear mid-iteration after rows lose influence
        # (weights -> 0); let RankDeficientError propagate to the caller.
        beta, info_k = _solve(A, y, w, pen, rcond, allow_rank_deficient)
        info = info_k
        residual = y - A @ beta
        obj = huber_objective(residual, delta)
        iterations += 1
        history.append(obj)
        beta_norm_hist.append(float(np.max(np.abs(beta))))

        obj_scale = max(1.0, abs(history[0]))
        if obj > prev_obj + OBJECTIVE_SLACK * obj_scale:
            monotone = False

        step = float(np.max(np.abs(beta - prev_beta)))
        scale = max(1.0, float(np.max(np.abs(beta))))
        if step <= tol * scale:
            converged = True
            prev_beta = beta
            prev_obj = obj
            break
        prev_beta = beta
        prev_obj = obj

    final_w = huber_weight(residual, delta)
    coef = beta[1:] if fit_intercept else beta
    intercept = float(beta[0]) if fit_intercept else 0.0
    slope_part = beta[1:] if fit_intercept else beta
    penalized_obj = obj + 0.5 * float(lam) * float(np.sum(slope_part ** 2))

    return HuberResult(
        coef=coef,
        intercept=intercept,
        residual=residual,
        weights=final_w,
        objective=obj,
        penalized_objective=penalized_obj,
        iterations=iterations,
        converged=converged,
        objective_decreased=monotone,
        max_iter=max_iter,
        tol=tol,
        delta=delta,
        rank=info["rank"],
        rank_deficient=info["rank_deficient"],
        nullspace_dim=info["nullspace_dim"],
        smallest_singular_value=info["smallest_sv"],
        singular_value_threshold=info["threshold"],
        singular_values=info["singular_values"],
        objective_history=history,
        coef_norm_history=beta_norm_hist,
        warnings=warns,
    )


def ols_fit(X, y, *, fit_intercept=True, rcond=None,
            allow_rank_deficient=False, delta=1.345):
    """Ordinary (optionally ridge-penalized) least squares baseline.

    Uses the same SVD solver as IRLS so rank detection semantics are
    identical. ``delta`` is only used to report the Huber objective at the OLS
    solution for like-for-like comparison.
    """
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    A = _build_design(X, fit_intercept)
    n, p_design = A.shape
    pen = np.zeros(p_design, dtype=np.float64)  # OLS: no penalty at all

    warns = []
    beta, info = _solve(A, y, None, pen, rcond, allow_rank_deficient)
    if info["rank_deficient"]:
        msg = (
            f"design matrix is rank deficient (rank {info['rank']}/"
            f"{p_design}); using SVD minimum-norm solution"
        )
        warns.append(msg)
        warnings.warn(msg, RuntimeWarning, stacklevel=2)

    residual = y - A @ beta
    coef = beta[1:] if fit_intercept else beta
    intercept = float(beta[0]) if fit_intercept else 0.0
    return OLSResult(
        coef=coef,
        intercept=intercept,
        residual=residual,
        sse=float(0.5 * np.sum(residual ** 2)),
        huber_objective=huber_objective(residual, delta),
        rank=info["rank"],
        rank_deficient=info["rank_deficient"],
        nullspace_dim=info["nullspace_dim"],
        smallest_singular_value=info["smallest_sv"],
        singular_value_threshold=info["threshold"],
        singular_values=info["singular_values"],
        warnings=warns,
    )
