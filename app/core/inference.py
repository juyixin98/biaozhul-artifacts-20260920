"""Weighted OLS with cluster-robust (sandwich) inference.

Implements exactly the standard texts (Cameron & Miller 2015; Wooldridge):

    beta_hat = (X'W X)^-1 X'W y
    meat     = sum_g X_g' W_g u_g u_g' W_g X_g
    bread    = (X'W X)^-1
    V_CR1    = G/(G-1) * (n-1)/(n-p) * bread @ meat @ bread

Standard errors are clustered on the panel unit (object identity). A unit's
weight is fixed across periods before this module is reached, so the same
weight multiplies both of its first-differenced observations.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats

from app.contracts.models import FailureCategory
from app.core.errors import EstimationError


@dataclass(frozen=True)
class OLSResult:
    beta: np.ndarray
    vcov: np.ndarray
    se: np.ndarray
    resid: np.ndarray
    rank: int
    n: int
    p: int
    n_clusters: int
    cluster_adjustment: str


def weighted_ols_cluster(
    x: np.ndarray,
    y: np.ndarray,
    weights: np.ndarray,
    clusters: np.ndarray,
    *,
    cluster_adjustment: str = "crv1",
    coef_names: list[str] | None = None,
) -> OLSResult:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    weights = np.asarray(weights, dtype=float)
    n, p = x.shape

    if n == 0:
        raise EstimationError(FailureCategory.NO_ESTIMABLE_UNITS, "no rows reached the estimator")
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise EstimationError(FailureCategory.NON_FINITE_OUTCOME, "non-finite values reached the estimator")
    if np.any(weights < 0) or not np.all(np.isfinite(weights)):
        raise EstimationError(FailureCategory.INVALID_WEIGHT, "weights must be finite and non-negative")

    sw = np.sqrt(weights)
    xw = x * sw[:, None]
    yw = y * sw

    # Economy SVD lets us detect genuine rank deficiency instead of silently
    # projecting a collinear design onto an arbitrary solution.
    u, s, vt = np.linalg.svd(xw, full_matrices=False)
    tol = max(n, p) * np.finfo(float).eps * (s[0] if s.size else 0.0)
    rank = int(np.sum(s > tol))
    if rank < p:
        names = coef_names or [f"x{i}" for i in range(p)]
        zeroish = [names[i] for i, sv in enumerate(s) if sv <= tol] if len(s) == p else []
        detail = f"design rank {rank} < {p} columns"
        if zeroish:
            detail += f"; collinear/zero columns: {zeroish}"
        raise EstimationError(FailureCategory.SINGULAR_DESIGN, detail)

    xpx_inv = (vt.T / (s**2)) @ vt
    beta = xpx_inv @ xw.T @ yw
    resid = y - x @ beta

    if n - rank <= 0:
        # The fitted values (hence the point estimate) are exactly determined,
        # but there is no residual variation, so any variance -- and therefore
        # clustered standard errors -- is undefined (0/0), not zero.
        raise EstimationError(
            FailureCategory.NO_RESIDUAL_DEGREES,
            f"saturated design: n={n} equals rank={rank}, leaving 0 residual degrees of freedom; "
            "point estimate is identified but standard errors are not",
        )

    fitted_w_resid = weights * resid  # W u

    unique_clusters, cluster_index = np.unique(clusters, return_inverse=True)
    g = len(unique_clusters)
    if g < 2:
        raise EstimationError(
            FailureCategory.INSUFFICIENT_CLUSTERS,
            f"cluster-robust inference needs >= 2 clusters (units), found {g}",
        )

    meat = np.zeros((p, p), dtype=float)
    for c in range(g):
        rows = cluster_index == c
        # X_g' W_g u_g  (a p-vector); outer product forms the cluster's meat.
        score = x[rows].T @ fitted_w_resid[rows]
        meat += np.outer(score, score)

    if cluster_adjustment == "crv1":
        small_sample = (g / (g - 1.0)) * ((n - 1.0) / (n - p))
    elif cluster_adjustment == "cr0":
        small_sample = 1.0
    else:  # pragma: no cover - guarded at config boundary
        raise ValueError(f"unknown cluster adjustment {cluster_adjustment!r}")

    vcov = small_sample * (xpx_inv @ meat @ xpx_inv)
    diag = np.diag(vcov)
    if np.any(diag <= 0):
        raise EstimationError(FailureCategory.SINGULAR_DESIGN, "non-positive variance after clustering")
    se = np.sqrt(diag)

    return OLSResult(
        beta=beta,
        vcov=vcov,
        se=se,
        resid=resid,
        rank=rank,
        n=n,
        p=p,
        n_clusters=g,
        cluster_adjustment=cluster_adjustment,
    )


def coefficient_inference(
    ols: OLSResult,
    index: int,
    *,
    alpha: float,
) -> dict[str, float | int]:
    """Wrap one coefficient with cluster-robust SE, t-test and CI.

    The reference distribution is t with G-1 degrees of freedom (the standard
    cluster asymptotics), which is intentionally conservative for few units.
    """
    est = float(ols.beta[index])
    se = float(ols.se[index])
    dof = ols.n_clusters - 1
    t_stat = est / se if se > 0 else float("nan")
    p_value = 2.0 * stats.t.sf(abs(t_stat), df=dof)
    crit = float(stats.t.ppf(1.0 - alpha / 2.0, df=dof))
    return {
        "value": est,
        "se": se,
        "ci_low": est - crit * se,
        "ci_high": est + crit * se,
        "t_stat": float(t_stat),
        "p_value": float(p_value),
        "dof": dof,
    }


def wald_test(ols: OLSResult, indices: list[int], values: np.ndarray | None = None) -> tuple[float, float]:
    """Wald chi2/F-style joint test of R beta = q.

    Returns (statistic, p_value) compared against an F with (r, G-1) degrees of
    freedom -- the cluster-aware version (each cluster supplies one effective
    draw, so the denominator dof is G-1, not n-p).
    """
    r = len(indices)
    if r == 0:
        return float("nan"), float("nan")
    rmat = np.zeros((r, ols.p))
    for row, col in enumerate(indices):
        rmat[row, col] = 1.0
    q = np.zeros(r) if values is None else np.asarray(values, dtype=float)
    diff = rmat @ ols.beta - q
    v_sub = rmat @ ols.vcov @ rmat.T
    w = float(diff @ np.linalg.inv(v_sub) @ diff)
    # Cluster-robust F: statistic W/r, reference F(r, G-1).
    f_stat = w / r
    p_value = float(stats.f.sf(f_stat, r, ols.n_clusters - 1))
    return f_stat, p_value
