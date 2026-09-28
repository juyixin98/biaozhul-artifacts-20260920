"""Independent reference regression.

This is a deliberately *separate code path* from ``kernel.py``:

* ``kernel.py`` works on within-object first differences (2 rows -> 1).
* This module works on the **levels** two-way fixed-effects-style regression

      y_it = a + b * treated_i + g * post_t + tau * (treated_i * post_t) + e_it

  with its own design-matrix construction and its own object-clustered CR1
  sandwich (each object contributes TWO rows to its cluster score). Agreement of
  the two independent derivations is evidence, not a tautology.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import stats

from .contracts import ReferenceRegression
from .panel import AlignedObject

PARAM_NAMES = ("intercept", "treated", "post", "treated_x_post")


def levels_reference_regression(
    aligned: list[AlignedObject],
    expected_did: float,
) -> ReferenceRegression:
    """Closed-form OLS on levels, clustered by object. Independent of kernel."""
    rows_x: list[np.ndarray] = []
    rows_y: list[float] = []
    cluster_of_row: list[int] = []

    for g, obj in enumerate(aligned):
        d = 1.0 if obj.group == "treated" else 0.0
        # pre period row: post = 0
        rows_x.append(np.array([1.0, d, 0.0, 0.0]))
        rows_y.append(obj.pre_y)
        cluster_of_row.append(g)
        # post period row: post = 1
        rows_x.append(np.array([1.0, d, 1.0, d]))
        rows_y.append(obj.post_y)
        cluster_of_row.append(g)

    x = np.vstack(rows_x)
    y = np.asarray(rows_y, dtype=float)
    clusters = np.asarray(cluster_of_row)
    g_total = len(aligned)

    # beta = (X'X)^-1 X'y  (plain OLS; weights are fixed at 1.0)
    xtx = x.T @ x
    xtx_inv = np.linalg.inv(xtx)
    beta = xtx_inv @ (x.T @ y)
    resid = y - x @ beta

    # Cluster meat: sum over objects of (X_g' e_g)(X_g' e_g)'
    meat = np.zeros((4, 4))
    for g in range(g_total):
        idx = clusters == g
        score = x[idx].T @ resid[idx]
        meat += np.outer(score, score)

    cr1 = g_total / (g_total - 1) if g_total > 1 else float("nan")
    cov = cr1 * (xtx_inv @ meat @ xtx_inv)
    se_tau = math.sqrt(float(cov[3, 3]))
    df = g_total - 1
    t_tau = float(beta[3] / se_tau) if se_tau > 0 else float("nan")
    p_tau = float(2.0 * stats.t.sf(abs(t_tau), df)) if math.isfinite(t_tau) else float("nan")

    return ReferenceRegression(
        model="OLS levels: y = a + b*treated + g*post + tau*(treated*post), "
        "object-clustered CR1 SE (independent code path, 2 rows/object)",
        did_coefficient=float(beta[3]),
        standard_error=se_tau,
        p_value=p_tau,
        coefficients={name: float(beta[i]) for i, name in enumerate(PARAM_NAMES)},
        max_abs_did_discrepancy=float(abs(beta[3] - expected_did)),
    )
