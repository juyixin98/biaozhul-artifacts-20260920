"""Estimation kernel.

* Weighted four cell means with weights FIXED per object (same weight pre/post).
* Difference-in-differences decomposition (hand-checkable).
* Object-clustered standard errors (CR1, Liang-Zeger / Stata pweight convention)
  via the equivalent two-period within regression of the first difference
  delta_i = post - pre on a constant and a treated indicator. With a balanced
  two-period panel this regression's treated coefficient is exactly the DID;
  clustering by object is equivalent to clustering the within-differenced score.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import stats

from .contracts import (
    CellMean,
    ClusteredSE,
    Decomposition,
    Failure,
    FailureCategory,
    Severity,
)
from .panel import AlignedObject, PanelBuildResult


@dataclass(frozen=True)
class KernelResult:
    decomposition: Decomposition
    clustered_se: ClusteredSE
    failures: list


def _cell(group: str, period: int, objs: list[AlignedObject], attr: str) -> CellMean:
    w = np.array([o.weight for o in objs], dtype=float)
    y = np.array([getattr(o, attr) for o in objs], dtype=float)
    wsum = float(w.sum())
    mean = float((w * y).sum() / wsum) if wsum > 0 else float("nan")
    return CellMean(
        group=group, period=period, n_objects=len(objs), mean=mean, sum_weights=wsum
    )


def estimate_did(
    panel: PanelBuildResult, pre_period: int, post_period: int
) -> KernelResult | None:
    """Return decomposition + clustered SE, or None (failure already recorded)."""
    failures: list[Failure] = list(panel.failures)
    treated = panel.by_group("treated")
    control = panel.by_group("control")

    if not treated or not control:
        missing = []
        if not treated:
            missing.append("treated")
        if not control:
            missing.append("control")
        terminal = Failure(
            category=FailureCategory.DEGENERATE_DESIGN,
            severity=Severity.ERROR,
            message=f"No observations in {missing} cell(s) after balancing; "
            "DID is not identified.",
        )
        failures.append(terminal)
        panel.failures.append(terminal)
        return None

    tp_pre = _cell("treated", pre_period, treated, "pre_y")
    tp_post = _cell("treated", post_period, treated, "post_y")
    ct_pre = _cell("control", pre_period, control, "pre_y")
    ct_post = _cell("control", post_period, control, "post_y")

    treated_change = tp_post.mean - tp_pre.mean
    control_change = ct_post.mean - ct_pre.mean
    did = treated_change - control_change

    decomposition = Decomposition(
        treated_pre=tp_pre,
        treated_post=tp_post,
        control_pre=ct_pre,
        control_post=ct_post,
        treated_change=treated_change,
        control_change=control_change,
        did=did,
    )

    clustered = _clustered_se(treated, control, did, failures)
    return KernelResult(
        decomposition=decomposition, clustered_se=clustered, failures=failures
    )


def _clustered_se(
    treated: list[AlignedObject],
    control: list[AlignedObject],
    did: float,
    failures: list[Failure],
) -> ClusteredSE:
    """CR1 object-clustered SE for the DID coefficient.

    WLS of delta_i = a + tau * D_i + e_i, one row per object (each object is one
    cluster after within-differencing), fixed per-object weights. Cluster score
    s_i = w_i * x_i * e_i (pweight convention); CR1 factor G/(G-1);
    reference t distribution with df = G - 1.
    """
    objs = treated + control
    n = len(objs)
    d = np.array([o.delta for o in objs], dtype=float)
    x = np.array([1.0 if o.group == "treated" else 0.0 for o in objs])
    w = np.array([o.weight for o in objs], dtype=float)

    X = np.column_stack([np.ones(n), x])
    XtW = X.T * w
    A = np.linalg.inv(XtW @ X)
    beta = A @ (XtW @ d)
    resid = d - X @ beta

    meat = np.zeros((2, 2))
    for i in range(n):
        s_i = w[i] * X[i] * resid[i]          # cluster score (one object each)
        meat += np.outer(s_i, s_i)
    g = n
    cr1 = g / (g - 1) if g > 1 else float("nan")
    var_tau = float(cr1 * (A @ meat @ A)[1, 1])

    n_t, n_c = len(treated), len(control)
    if n_t < 2 or n_c < 2 or var_tau <= 0.0 or not math.isfinite(var_tau):
        failures.append(
            Failure(
                category=FailureCategory.CLUSTER_VARIANCE_DEGENERATE,
                severity=Severity.WARNING,
                message="Clustered variance is degenerate (fewer than two "
                "clusters in a group or zero residual variation); the point "
                "estimate is returned without a reliable standard error.",
                detail={"n_treated_clusters": n_t, "n_control_clusters": n_c},
            )
        )
        return ClusteredSE(
            method="CR1 object-clustered sandwich on within-differenced 2-period panel "
            "(df = n_objects - 1)",
            did_standard_error=float("nan"),
            t_stat=float("nan"),
            p_value=float("nan"),
            ci95_low=float("nan"),
            ci95_high=float("nan"),
            n_objects=n,
            n_clusters=g,
            df=g - 1,
        )

    se = math.sqrt(var_tau)
    tstat = did / se
    df = g - 1
    p = float(2.0 * stats.t.sf(abs(tstat), df))
    tcrit = float(stats.t.ppf(0.975, df))
    return ClusteredSE(
        method="CR1 object-clustered sandwich on within-differenced 2-period panel "
        "(df = n_objects - 1)",
        did_standard_error=se,
        t_stat=tstat,
        p_value=p,
        ci95_low=did - tcrit * se,
        ci95_high=did + tcrit * se,
        n_objects=n,
        n_clusters=g,
        df=df,
    )
