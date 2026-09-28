"""Evidence and diagnostics.

These routines never decide the point estimate; they characterize how much the
data should be trusted. Two diagnostics matter most for DID:

* :func:`pretrend_diagnostic` -- tests for differential trends in the
  *pre-treatment* periods. A significant pre-trend is evidence against the
  parallel-trends assumption. The inverse is not true: passing the pre-trend
  test does not prove parallel trends (absent evidence is not evidence of
  absence, and unobserved confounding can still begin at treatment).
* :func:`contamination_evidence` -- enumerates units whose treatment state
  pollutes the comparison group.

Diagnostics return structured :class:`Diagnostic` objects with explicit levels
instead of raising, except where the request itself demands a hard rejection
(handled by the estimator).
"""
from __future__ import annotations

import numpy as np

from app.contracts.models import (
    Diagnostic,
    DiagnosticLevel,
    FailureCategory,
)
from app.core.alignment import UnitRecord
from app.core.inference import OLSResult, weighted_ols_cluster, wald_test

PRETREND_CAVEAT = (
    "A pre-trend test can only falsify the parallel-trends assumption; "
    "a non-significant result does NOT prove parallel trends "
    "(limited power, and confounding may begin at treatment onset)."
)


def contamination_evidence(
    units: dict[str, UnitRecord],
    treated_uids: list[str],
    control_uids: list[str],
) -> list[Diagnostic]:
    """Flag any control unit that is treated in *any* observed period."""
    diags: list[Diagnostic] = []
    dirty: list[tuple[str, list[int]]] = []
    treated_set = set(treated_uids)
    for uid in control_uids:
        periods_on = [t for t, on in sorted(units[uid].treated.items()) if on]
        if periods_on:
            dirty.append((uid, periods_on))
    if dirty:
        listing = "; ".join(f"{u} treated at {p}" for u, p in dirty)
        diags.append(
            Diagnostic(
                name="control_contamination",
                level=DiagnosticLevel.FAILED,
                message=(
                    f"{len(dirty)} control unit(s) are treated in some observed period: {listing}. "
                    "Such units are excluded from the clean control group, not silently kept."
                ),
                caveat="Contaminated controls bias DID toward zero (or past it); check coding and cohort dates.",
            )
        )
    else:
        diags.append(
            Diagnostic(
                name="control_contamination",
                level=DiagnosticLevel.OK,
                message="Every control unit is untreated in all observed periods.",
            )
        )
    # Units outside both lists that are ever treated are reported too, so the
    # evidence trail is complete even when classification already dropped them.
    outside = [
        uid
        for uid, rec in units.items()
        if uid not in treated_set and uid not in {u for u, _ in dirty} and rec.ever_treated()
    ]
    for uid in outside:
        periods_on = [t for t, on in sorted(units[uid].treated.items()) if on]
        diags.append(
            Diagnostic(
                name=f"treated_outside_design:{uid}",
                level=DiagnosticLevel.WARNING,
                message=(
                    f"unit {uid} is treated at {periods_on} but did not enter the 2x2 treated group "
                    "(e.g. always-treated or reversed path); see excluded records."
                ),
            )
        )
    return diags


def pretrend_diagnostic(
    units: dict[str, UnitRecord],
    treated_uids: list[str],
    control_uids: list[str],
    onset: int,
    *,
    cluster_adjustment: str = "crv1",
    alpha: float = 0.05,
) -> Diagnostic:
    """Test differential linear trends in strictly pre-treatment periods.

    Fit, on observations with ``period < onset``:

        y_it = a + b·T_i + c·t + d·(T_i · t) + e

    and test ``d = 0`` (different slopes) with unit-clustered standard errors.
    Requires at least two distinct pre-treatment periods; with one pre period
    the design is unidentified and the result is reported INDETERMINATE rather
    than pretending the assumption holds.
    """
    pre_periods = sorted({t for rec in units.values() for t in rec.y if t < onset})
    if len(pre_periods) < 2:
        return Diagnostic(
            name="pretrend_slope",
            level=DiagnosticLevel.INDETERMINATE,
            message=(
                f"Only {len(pre_periods)} pre-treatment period(s) {pre_periods} precede onset {onset}; "
                "a differential-trend test needs at least two. No pre-trend statement is possible."
            ),
            caveat=PRETREND_CAVEAT,
        )

    t0 = min(pre_periods)
    rows_y: list[float] = []
    rows_x: list[list[float]] = []
    rows_w: list[float] = []
    clusters: list[str] = []
    for uid in treated_uids + control_uids:
        rec = units[uid]
        is_t = 1.0 if uid in set(treated_uids) else 0.0
        for t in pre_periods:
            if t not in rec.y:
                # Missing period stays missing: no imputation in diagnostics.
                continue
            tt = float(t - t0)
            rows_y.append(rec.y[t])
            rows_x.append([1.0, is_t, tt, is_t * tt])
            rows_w.append(rec.fixed_weight)
            clusters.append(uid)

    try:
        ols: OLSResult = weighted_ols_cluster(
            np.array(rows_x),
            np.array(rows_y),
            np.array(rows_w),
            np.array(clusters, dtype=object),
            cluster_adjustment=cluster_adjustment,
            coef_names=["intercept", "treated", "time", "treated_x_time"],
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic must not crash the run
        return Diagnostic(
            name="pretrend_slope",
            level=DiagnosticLevel.INDETERMINATE,
            message=f"pre-trend regression could not be fit: {exc}",
            caveat=PRETREND_CAVEAT,
        )

    # d is coefficient index 3.
    f_stat, p_value = wald_test(ols, [3])
    d = float(ols.beta[3])
    if p_value < alpha:
        level = DiagnosticLevel.FAILED
        message = (
            f"Reject equal pre-treatment slopes (interaction d={d:.4g}, F={f_stat:.3f}, p={p_value:.4f}): "
            "treated and control groups were already trending differently before treatment."
        )
    else:
        level = DiagnosticLevel.WARNING  # never OK-as-proof: remains a caveated non-rejection
        message = (
            f"No differential pre-treatment slope detected (d={d:.4g}, F={f_stat:.3f}, p={p_value:.4f}). "
            "This is consistent with parallel trends but cannot establish it."
        )
    return Diagnostic(
        name="pretrend_slope",
        level=level,
        message=message,
        statistic=float(f_stat),
        p_value=float(p_value),
        caveat=PRETREND_CAVEAT,
    )


def cluster_count_warning(n_clusters: int, min_clusters: int) -> Diagnostic | None:
    if n_clusters < min_clusters:
        return Diagnostic(
            name="cluster_count",
            level=DiagnosticLevel.FAILED,
            message=(
                f"Only {n_clusters} clusters (units); cluster-robust inference is unreliable below "
                f"{min_clusters}. Treat standard errors as descriptive only."
            ),
        )
    if n_clusters < 30:
        return Diagnostic(
            name="cluster_count",
            level=DiagnosticLevel.WARNING,
            message=(
                f"{n_clusters} clusters: CRV1 with t(G-1) critical values is used, but with few clusters "
                "inference can still over-reject; consider CRV3/wild bootstrap in real analyses."
            ),
        )
    return Diagnostic(
        name="cluster_count",
        level=DiagnosticLevel.OK,
        message=f"{n_clusters} clusters; standard errors clustered on unit with CRV1 adjustment.",
    )
