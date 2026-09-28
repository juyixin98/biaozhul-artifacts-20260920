"""Evidence and diagnostics.

Two diagnostics, kept explicitly separate from identification:

1. Treatment contamination ("处理污染"): a *control* object that is itself treated
   in the post period cannot serve as a counterfactual, and a *treated* object
   already treated in the pre period is not a clean 2x2 switcher. Both are moved
   to ``excluded_records`` with an explicit category before estimation.

2. Pre-trend ("平行趋势"): with TWO earlier (untreated) periods we can *diagnose*
   whether treated and control outcomes moved similarly before treatment. This
   can reject the parallel-trends assumption on the observed window; it can
   NEVER prove it (the counterfactual post trend is unobserved).
"""
from __future__ import annotations

import math

import numpy as np
from scipy import stats

from .contracts import (
    ContaminationReport,
    ExcludedRecord,
    Failure,
    FailureCategory,
    PretrendDiagnostic,
    Severity,
)
from .panel import AlignedObject, PanelBuildResult, build_balanced_panel


# --------------------------------------------------------------------------- #
# Contamination screening
# --------------------------------------------------------------------------- #
def screen_contamination(
    panel: PanelBuildResult,
    allow_contaminated: bool,
    pre_period: int | None = None,
    post_period: int | None = None,
) -> ContaminationReport:
    """Move contaminated / early-treated objects out of the estimation sample."""
    aligned_periods = (
        [p for p in (pre_period, post_period) if p is not None]
    )
    contaminated_control: list[str] = []
    treated_pre: list[str] = []

    for oid, obj in list(panel.aligned.items()):
        reasons: list[FailureCategory] = []
        if obj.group == "control" and obj.post_was_treated:
            reasons.append(FailureCategory.CONTAMINATED_CONTROL)
            contaminated_control.append(oid)
        if obj.group == "treated" and obj.pre_was_treated:
            reasons.append(FailureCategory.PRETREND_TREATED_EARLY)
            treated_pre.append(oid)
        if not reasons:
            continue

        detail = {
            "pre_was_treated": obj.pre_was_treated,
            "post_was_treated": obj.post_was_treated,
            "group": obj.group,
        }
        panel.excluded.append(
            ExcludedRecord(
                object_id=oid,
                reasons=reasons,
                periods_present=aligned_periods,
                detail=detail,
            )
        )
        del panel.aligned[oid]

        for reason in reasons:
            severity = Severity.WARNING if allow_contaminated else Severity.ERROR
            msg = (
                f"Object {oid} is in the control group but is treated in the "
                "post period (contaminated control); excluded from the control "
                "counterfactual."
                if reason is FailureCategory.CONTAMINATED_CONTROL
                else f"Object {oid} is in the treated group but already treated "
                "in the pre period; not a clean 2x2 switcher, excluded."
            )
            panel.failures.append(
                Failure(
                    category=reason,
                    severity=severity,
                    message=msg,
                    object_ids=[oid],
                    detail=detail,
                )
            )

    return ContaminationReport(
        contaminated_control_ids=sorted(contaminated_control),
        treated_pre_ids=sorted(treated_pre),
        clean=not contaminated_control and not treated_pre,
    )


# --------------------------------------------------------------------------- #
# Pre-trend diagnostic
# --------------------------------------------------------------------------- #
def pretrend_diagnostic(
    observations, earlier_period: int | None, pre_period: int
) -> PretrendDiagnostic:
    """Compare treated vs control change over two earlier untreated periods.

    Uses an independent balanced alignment on the pre window. Only objects that
    are untreated in BOTH earlier periods participate (clean placebo window).
    """
    if earlier_period is None:
        return PretrendDiagnostic(
            feasible=False,
            conclusion="not_tested",
            note="No second pre-treatment period was provided, so the parallel "
            "trends assumption cannot even be diagnosed (and can never be "
            "proved: the counterfactual post trend is unobserved).",
        )

    if earlier_period == pre_period:
        return PretrendDiagnostic(
            feasible=False,
            conclusion="invalid",
            note=f"earlier_period equals pre_period ({pre_period}).",
        )

    present = {o.period for o in observations}
    if earlier_period not in present:
        return PretrendDiagnostic(
            feasible=False,
            conclusion="not_tested",
            note=f"Earlier period {earlier_period} is absent from the data.",
        )

    pre_panel = build_balanced_panel(observations, earlier_period, pre_period)
    if pre_panel.failures and any(
        f.category is FailureCategory.DEGENERATE_DESIGN for f in pre_panel.failures
    ):
        return PretrendDiagnostic(
            feasible=False,
            conclusion="not_tested",
            note="Balanced pre window could not be built.",
        )

    # Clean placebo: untreated in both pre periods.
    clean = [
        o
        for o in pre_panel.aligned.values()
        if not o.pre_was_treated and not o.post_was_treated
    ]
    treated = [o for o in clean if o.group == "treated"]
    control = [o for o in clean if o.group == "control"]

    if len(treated) < 2 or len(control) < 2:
        return PretrendDiagnostic(
            feasible=False,
            conclusion="not_tested",
            note="Fewer than two clean (untreated) objects per group in the pre "
            "window; pre-trend difference is not estimable.",
        )

    deltas = np.array([o.delta for o in clean])
    x = np.array([1.0 if o.group == "treated" else 0.0 for o in clean])
    n = len(clean)
    X = np.column_stack([np.ones(n), x])
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ (X.T @ deltas)
    resid = deltas - X @ beta

    # CR1 object-clustered sandwich on the pre-window first differences.
    meat = np.zeros((2, 2))
    for i in range(n):
        s_i = X[i] * resid[i]
        meat += np.outer(s_i, s_i)
    g = n
    cov = (g / (g - 1)) * (XtX_inv @ meat @ XtX_inv)
    diff = float(beta[1])
    se = math.sqrt(float(cov[1, 1]))

    if se <= 0.0:
        # Zero within-group residual variation: the test statistic is not
        # finite. An observed difference of exactly 0 is still not a *proof*
        # of parallel trends.
        return PretrendDiagnostic(
            feasible=True,
            pretrend_difference=diff,
            standard_error=0.0,
            t_stat=None,
            p_value=None,
            conclusion="not_rejected" if diff == 0.0 else "parallel_trends_rejected",
            note=(
                "Residual variation in the pre window is exactly zero, so the "
                "test statistic is undefined; the observed pre-trend difference "
                f"is {diff:.4g}. This synthetic edge case neither proves nor "
                "formally tests parallel trends."
            ),
        )

    tstat = diff / se
    df = g - 1
    pval = float(2.0 * stats.t.sf(abs(tstat), df))

    alpha = 0.05
    if pval < alpha:
        conclusion = "parallel_trends_rejected"
        note = (
            f"Treated and control moved differently before treatment "
            f"(difference {diff:.4g}, p={pval:.4g} < {alpha}). The parallel "
            "trends assumption is REJECTED on this pre window. A 2x2 DID is not "
            "credible without adjustment; this test diagnoses but cannot prove "
            "parallel trends."
        )
    else:
        conclusion = "not_rejected"
        note = (
            f"No statistically detectable pre-trend difference "
            f"(difference {diff:.4g}, p={pval:.4g}). This does NOT prove "
            "parallel trends -- it only fails to reject on the observed pre "
            "window; the counterfactual post trend remains unobserved."
        )

    return PretrendDiagnostic(
        feasible=True,
        pretrend_difference=diff,
        standard_error=se,
        t_stat=float(tstat),
        p_value=pval,
        conclusion=conclusion,
        note=note,
    )
