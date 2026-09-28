"""Independent reference implementation.

This module is an ORACLE. It deliberately does NOT import anything from
``app.core``. It re-derives the estimators from first principles using only
NumPy primitives (``np.linalg.lstsq`` and explicit loops), along a different
code path than the SVD-based core, so agreement is genuine cross-validation
rather than a tautology.

If the core and this file share a bug, that is a residual risk; the
hand-computed constants in ``reference_expected.py`` guard against that for the
point estimates and decomposition, which are simple enough to do on paper.
"""
from __future__ import annotations

import numpy as np


def ref_weighted_mean(values: list[float], weights: list[float]) -> float:
    v = np.asarray(values, dtype=float)
    w = np.asarray(weights, dtype=float)
    return float(np.sum(w * v) / np.sum(w))


def ref_2x2_did(
    treated_pre: list[tuple[float, float]],
    treated_post: list[tuple[float, float]],
    control_pre: list[tuple[float, float]],
    control_post: list[tuple[float, float]],
) -> dict[str, float]:
    """Reference four-cell decomposition. Each argument is a list of (y, w)."""
    tp = ref_weighted_mean([y for y, _ in treated_pre], [w for _, w in treated_pre])
    tpo = ref_weighted_mean([y for y, _ in treated_post], [w for _, w in treated_post])
    cp = ref_weighted_mean([y for y, _ in control_pre], [w for _, w in control_pre])
    cpo = ref_weighted_mean([y for y, _ in control_post], [w for _, w in control_post])
    return {
        "treat_pre": tp,
        "treat_post": tpo,
        "control_pre": cp,
        "control_post": cpo,
        "treat_change": tpo - tp,
        "control_change": cpo - cp,
        "did": (tpo - tp) - (cpo - cp),
    }


def ref_cluster_wls(
    x: np.ndarray,
    y: np.ndarray,
    w: np.ndarray,
    clusters: np.ndarray,
) -> dict[str, np.ndarray | int]:
    """Weighted least squares with a CRV1 cluster sandwich, written from scratch.

    Uses normal equations + ``np.linalg.solve`` (the core uses an SVD inverse)
    and a dictionary accumulation of the cluster meat (the core uses
    ``np.unique``). Different mechanics, same estimator.
    """
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    w = np.asarray(w, float)
    n, p = x.shape
    sw = np.sqrt(w)
    xw = x * sw[:, None]
    yw = y * sw
    xtx = xw.T @ xw
    xty = xw.T @ yw
    beta = np.linalg.solve(xtx, xty)
    resid = y - x @ beta

    # Accumulate X_g' W_g u_g per cluster with an explicit dict.
    scores: dict[str, np.ndarray] = {}
    for i in range(n):
        key = str(clusters[i])
        contribution = x[i] * (w[i] * resid[i])
        scores[key] = contribution if key not in scores else scores[key] + contribution
    meat = np.zeros((p, p))
    for s in scores.values():
        meat += np.outer(s, s)

    g = len(scores)
    factor = (g / (g - 1.0)) * ((n - 1.0) / (n - p))
    xtx_inv = np.linalg.inv(xtx)
    vcov = factor * (xtx_inv @ meat @ xtx_inv)
    se = np.sqrt(np.diag(vcov))
    return {"beta": beta, "vcov": vcov, "se": se, "n_clusters": g, "n": n, "p": p}


def ref_fd_did(
    dy: list[float],
    treated: list[int],
    weights: list[float],
    clusters: list[str],
) -> dict[str, float | int]:
    """Reference first-difference DID: dY_i = a + b T_i + e_i."""
    x = np.column_stack([np.ones(len(dy)), np.asarray(treated, float)])
    fit = ref_cluster_wls(x, np.asarray(dy, float), np.asarray(weights, float), np.asarray(clusters))
    return {
        "intercept": float(fit["beta"][0]),
        "did": float(fit["beta"][1]),
        "se": float(fit["se"][1]),
        "n_clusters": int(fit["n_clusters"]),
    }


def ref_event_study(
    rows: list[tuple[str, int, float, float, int | None]],
    unit_ids: list[str],
    periods: list[int],
    window: list[int],
) -> dict[int, float]:
    """Reference TWFE event study by explicit dummy WLS via ``lstsq``.

    ``rows`` are (unit, period, y, weight, event_time_or_None). The -1 lead is
    omitted. This uses a deliberately different full-rank parametrization than
    the core: one intercept + ``T-1`` period dummies + ``N-1`` unit dummies +
    event dummies (the core omits the intercept and drops the first unit and
    first period). The event coefficients are invariant to that choice, so
    agreement cross-validates the estimate along an independent code path.
    """
    event_cols = [k for k in window if k != -1]
    sorted_units = sorted(unit_ids)
    sorted_periods = sorted(periods)
    u_idx = {u: j for j, u in enumerate(sorted_units)}
    t_idx = {t: j for j, t in enumerate(sorted_periods)}
    # columns: [intercept, period(1..T-1), unit(1..N-1), event...]
    n_per = len(sorted_periods) - 1
    n_unit = len(sorted_units) - 1
    p = 1 + n_per + n_unit + len(event_cols)
    x = np.zeros((len(rows), p))
    y = np.zeros(len(rows))
    sw_ = np.zeros(len(rows))
    epos = {k: j for j, k in enumerate(event_cols)}
    base = 1 + n_per + n_unit
    for r, (u, t, val, wt, k) in enumerate(rows):
        y[r] = val
        sw_[r] = np.sqrt(wt)
        x[r, 0] = 1.0
        if t_idx[t] > 0:
            x[r, 1 + (t_idx[t] - 1)] = 1.0
        if u_idx[u] > 0:
            x[r, 1 + n_per + (u_idx[u] - 1)] = 1.0
        if k is not None and k != -1:
            x[r, base + epos[k]] = 1.0
    beta, *_ = np.linalg.lstsq(x * sw_[:, None], y * sw_, rcond=None)
    return {k: float(beta[base + epos[k]]) for k in event_cols}
