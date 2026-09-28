"""Service orchestration: contracts in, contracts out.

This is the thin seam between HTTP and the estimation core. It runs the fixed
pipeline (align -> classify -> estimate -> diagnose) and is the single place
that assembles the auditable result envelope (decomposition, regression
cross-check, diagnostics, exclusion records, step trace).
"""
from __future__ import annotations

from app.config import CONFIG
from app.contracts.models import (
    CellMeans,
    Decomposition,
    DIDRequest,
    DIDResult,
    Diagnostic,
    DiagnosticLevel,
    Estimate,
    EventStudyRequest,
    EventStudyResult,
    EventTimePoint,
    FailureCategory,
    StepTrace,
    WeightPolicy,
)
from app.core.alignment import align_panel, classify_two_by_two
from app.core.estimation import (
    build_event_support,
    did_first_difference_regression,
    event_study_twfe,
    four_cell_decomposition,
)
from app.core.errors import EstimationError
from app.core.inference import coefficient_inference
from app.evidence.diagnostics import (
    cluster_count_warning,
    contamination_evidence,
    pretrend_diagnostic,
)

REGRESSION_AGREEMENT_TOL = 1e-10


def _attach_failure(err: EstimationError, request_id: str) -> dict:
    return {
        "request_id": request_id,
        "core_version": CONFIG.core_version,
        "failure_category": err.category,
        "message": err.message,
        "diagnostics": err.diagnostics,
        "excluded": err.excluded,
        "steps": err.steps,
    }


def run_did(request: DIDRequest) -> DIDResult:
    """Execute the full 2x2 DID pipeline. Raises EstimationError on rejection."""
    try:
        panel = align_panel(
            request.observations,
            balance=request.balance,
            weight_policy=request.weight_policy,
            pre_period=request.pre_period,
            post_period=request.post_period,
        )
        treated, control, excluded, steps = classify_two_by_two(panel)

        # Contamination is already recorded during classification as excluded
        # rows; the request decides whether that is a hard rejection.
        n_contam = sum(1 for e in excluded if e.reason is FailureCategory.CONTROL_GROUP_CONTAMINATED)
        if request.reject_on_contamination and n_contam:
            raise EstimationError(
                FailureCategory.CONTROL_GROUP_CONTAMINATED,
                f"{n_contam} control unit(s) are treated in an observed period; "
                "rejecting because reject_on_contamination=true. Set it false to exclude-and-continue.",
                excluded=excluded,
                steps=steps,
            )

        # Estimators.
        four = four_cell_decomposition(
            panel,
            treated,
            control,
            n_contaminated=sum(
                1 for e in excluded if e.reason is FailureCategory.CONTROL_GROUP_CONTAMINATED
            ),
        )
        # The point estimate is the four-cell DID. Regression supplies
        # inference and an exact cross-check, but a saturated design (e.g. one
        # treated and one control) has no residual dof: the estimate is still
        # reported, with inference explicitly marked unavailable rather than
        # fabricated.
        inf: dict | None = None
        inference_error: EstimationError | None = None
        try:
            ols, dy, treat_ind, weights, clusters = did_first_difference_regression(
                panel,
                treated,
                control,
                cluster_adjustment=CONFIG.estimation.cluster_adjustment,
            )
            inf = coefficient_inference(ols, index=1, alpha=request.alpha)
        except EstimationError as exc:
            if exc.category is not FailureCategory.NO_RESIDUAL_DEGREES:
                raise
            inference_error = exc
            ols = None  # type: ignore[assignment]

        if inf is not None:
            # Internal cross-check: the regression coefficient on treated must
            # equal the hand-checkable four-cell DID to machine precision.
            if abs(inf["value"] - four.did) > REGRESSION_AGREEMENT_TOL:
                raise EstimationError(  # pragma: no cover - internal invariant
                    FailureCategory.SINGULAR_DESIGN,
                    f"internal inconsistency: regression DID {inf['value']} != four-cell DID {four.did}",
                    excluded=excluded,
                    steps=steps,
                )

        # Diagnostics.
        diag: list[Diagnostic] = []
        diag.extend(contamination_evidence(panel.units, treated, control))
        onset = panel.post_period  # in a 2x2 design treatment switches on at post
        diag.append(
            pretrend_diagnostic(
                panel.units,
                treated,
                control,
                onset=onset,
                cluster_adjustment=CONFIG.estimation.cluster_adjustment,
                alpha=request.alpha,
            )
        )
        n_clusters = len(treated) + len(control)
        if ols is not None:
            cluster_diag = cluster_count_warning(
                ols.n_clusters, CONFIG.estimation.min_clusters
            )
            if cluster_diag is not None:
                diag.append(cluster_diag)
            diag.append(
                Diagnostic(
                    name="decomposition_vs_regression",
                    level=DiagnosticLevel.OK,
                    message=(
                        f"four-cell DID {four.did:.10g} equals first-difference OLS coefficient "
                        f"{inf['value']:.10g} (|diff| <= {REGRESSION_AGREEMENT_TOL:g})"
                    ),
                    statistic=abs(inf["value"] - four.did),
                )
            )
        else:
            # Saturated design: report the point estimate but separate the
            # unavailable inference as its own INDETERMINATE conclusion.
            diag.append(
                Diagnostic(
                    name="inference_unavailable",
                    level=DiagnosticLevel.INDETERMINATE,
                    message=(
                        f"{inference_error.message}. The DID point estimate is the exact four-cell "
                        "difference, but no standard error, CI or p-value is reported."
                    ),
                    caveat="Add at least one more unit to either group to obtain residual variation.",
                )
            )
            diag.append(
                Diagnostic(
                    name="cluster_count",
                    level=DiagnosticLevel.FAILED,
                    message=(
                        f"only {n_clusters} units and a saturated design; cluster-robust inference "
                        "is undefined here"
                    ),
                )
            )

        n_obs = len(treated) + len(control)
        estimate = Estimate(
            name="att_did",
            value=float(four.did),
            se=inf["se"] if inf else None,
            ci_low=inf["ci_low"] if inf else None,
            ci_high=inf["ci_high"] if inf else None,
            t_stat=inf["t_stat"] if inf else None,
            p_value=inf["p_value"] if inf else None,
            dof=inf["dof"] if inf else None,
            n_units=n_obs,
            n_obs=2 * n_obs,
            n_clusters=n_clusters,
        )
        cells = CellMeans(
            treat_pre=four.treat_pre,
            treat_post=four.treat_post,
            control_pre=four.control_pre,
            control_post=four.control_post,
            n_treat=four.n_treat,
            n_control=four.n_control,
        )
        decomposition = Decomposition(
            cells=cells,
            treat_change=four.treat_change,
            control_change=four.control_change,
            did=four.did,
        )

        if inf is not None:
            estimate_detail = (
                f"four-cell DID={four.did:.6g}; first-difference OLS b={inf['value']:.6g} "
                f"se={inf['se']:.6g} clustered on unit (G={ols.n_clusters})"
            )
        else:
            estimate_detail = (
                f"four-cell DID={four.did:.6g}; inference unavailable (saturated design, "
                f"G={n_clusters}); point estimate only"
            )
        steps.append(
            StepTrace(
                step="estimate",
                detail=estimate_detail,
                n_units_in=len(panel.balanced_units),
                n_units_out=n_obs,
            )
        )

        return DIDResult(
            request_id=request.request_id,
            core_version=CONFIG.core_version,
            status="ok",
            estimate=estimate,
            decomposition=decomposition,
            diagnostics=diag,
            excluded=excluded,
            steps=steps,
            weight_policy=request.weight_policy,
            balance=request.balance,
        )
    except EstimationError:
        raise
    except Exception as exc:  # noqa: BLE001 - convert unexpected failures to a category
        raise EstimationError(
            FailureCategory.INVALID_REQUEST,
            f"unexpected estimation failure: {type(exc).__name__}: {exc}",
        ) from exc


def run_event_study(request: EventStudyRequest) -> EventStudyResult:
    """Event-time TWFE pipeline with an explicit support refusal."""
    try:
        panel = align_panel(
            request.observations,
            balance=request.balance,
            weight_policy=request.weight_policy,
        )
        excluded = list(panel.excluded)
        steps = list(panel.steps)

        support = build_event_support(
            panel.units,
            control_group=request.control_group,
            requested_min=request.min_event_time,
            requested_max=request.max_event_time,
        )

        ols, event_cols, rows, all_uids, all_periods = event_study_twfe(
            panel.units,
            support,
            weight_policy=request.weight_policy,
            cluster_adjustment=CONFIG.estimation.cluster_adjustment,
        )

        points: list[EventTimePoint] = []
        # cohort/unit counts per supported event time
        units_per_k: dict[int, set[str]] = {}
        cohorts_per_k: dict[int, set[int]] = {}
        for uid in all_uids:
            rec = panel.units.get(uid)
            if rec is None or rec.first_treat() is None:
                continue
            g = rec.first_treat()
            for t in rec.y:
                k = t - g
                if k in support.window:
                    units_per_k.setdefault(k, set()).add(uid)
                    cohorts_per_k.setdefault(k, set()).add(g)

        for k in support.window:
            if k == -1:
                points.append(
                    EventTimePoint(
                        event_time=k,
                        estimate=0.0,
                        se=0.0,
                        ci_low=0.0,
                        ci_high=0.0,
                        n_cohorts=len(cohorts_per_k.get(k, set())),
                        n_units=len(units_per_k.get(k, set())),
                        supported=True,
                    )
                )
                continue
            j = event_cols.index(k)
            inf = coefficient_inference(ols, index=j, alpha=request.alpha)
            points.append(
                EventTimePoint(
                    event_time=k,
                    estimate=inf["value"],
                    se=inf["se"],
                    ci_low=inf["ci_low"],
                    ci_high=inf["ci_high"],
                    n_cohorts=len(cohorts_per_k.get(k, set())),
                    n_units=len(units_per_k.get(k, set())),
                    supported=True,
                )
            )

        diag: list[Diagnostic] = []
        cluster_diag = cluster_count_warning(
            ols.n_clusters, CONFIG.estimation.min_clusters
        )
        if cluster_diag is not None:
            diag.append(cluster_diag)
        diag.append(
            Diagnostic(
                name="parallel_trends_scope",
                level=DiagnosticLevel.WARNING,
                message=(
                    "Leads are diagnostics for pre-trends only; they cannot prove parallel trends. "
                    "The -1 period is normalized to zero."
                ),
            )
        )

        steps.append(
            StepTrace(
                step="event_study",
                detail=(
                    f"TWFE over {len(rows)} rows, {len(all_uids)} units, {len(all_periods)} periods, "
                    f"window {support.window}, cohorts {sorted(support.cohorts)}"
                ),
                n_units_in=len(panel.units),
                n_units_out=len(all_uids),
            )
        )

        return EventStudyResult(
            request_id=request.request_id,
            core_version=CONFIG.core_version,
            status="ok",
            points=points,
            reference_period=-1,
            diagnostics=diag,
            excluded=excluded,
            steps=steps,
        )
    except EstimationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EstimationError(
            FailureCategory.INVALID_REQUEST,
            f"unexpected event-study failure: {type(exc).__name__}: {exc}",
        ) from exc
