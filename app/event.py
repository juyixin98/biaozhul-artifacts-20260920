"""Event-time alignment and descriptive event study.

Supported model (explicitly narrow):
  * a SINGLE common treatment cohort -- every treated object is first treated in
    the same calendar period;
  * a set of never-treated controls;
  * event time 0 = the common first-treatment period, event time -1 is the
    normalization period.

Anything outside this support is REFUSED, not silently approximated. In
particular staggered ("错时") adoption -- treated objects first treated in
different periods -- raises ``EVENT_STAGGER_UNSUPPORTED``: naive two-way-FE
event-study coefficients are known to be contaminated by forbidden comparisons
under staggered timing, so this service will not report them without an
explicitly supported estimator.

Identity alignment carries over: for each event-time point an object enters only
if it is observed in BOTH the target period and the normalization period; a
missing period is never zero.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from .contracts import (
    EventTimePoint,
    ExcludedRecord,
    Failure,
    FailureCategory,
    Severity,
)


def _point_estimate(treated_pairs, control_pairs):
    """DID at one event time from (y_target, y_ref) pairs, CR1 clustered SE.

    Returns (est, se, df, perfect_fit). ``perfect_fit`` distinguishes an exact
    fit (within-group residual variation is literally zero -> sandwich SE is
    exactly 0, a property of the synthetic data) from an unidentified variance
    (fewer than two clusters in a group -> SE is NaN).
    """
    pairs = treated_pairs + control_pairs
    n = len(pairs)
    d = np.array([t[0] - t[1] for t in pairs], dtype=float)
    x = np.array([1.0] * len(treated_pairs) + [0.0] * len(control_pairs))
    X = np.column_stack([np.ones(n), x])
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ (X.T @ d)
    resid = d - X @ beta
    meat = np.zeros((2, 2))
    for i in range(n):
        s_i = X[i] * resid[i]
        meat += np.outer(s_i, s_i)
    cr1 = n / (n - 1) if n > 1 else float("nan")
    var = cr1 * float((XtX_inv @ meat @ XtX_inv)[1, 1])
    est = float(beta[1])
    df = n - 1
    if len(treated_pairs) < 2 or len(control_pairs) < 2:
        return est, float("nan"), df, False
    if var <= 0.0:
        # >=2 clusters per group but zero residual variation: the finite-sample
        # clustered variance estimate is exactly 0 (perfect fit).
        return est, 0.0, df, True
    return est, math.sqrt(var), df, False


def run_event_study(observations, min_event_time: int, max_event_time: int):
    """Return (points, excluded, failures, summary). Status refused on stagger."""
    excluded: list[ExcludedRecord] = []
    failures: list[Failure] = []

    if min_event_time >= 0 or max_event_time < min_event_time:
        failures.append(
            Failure(
                category=FailureCategory.INVALID_REQUEST,
                severity=Severity.ERROR,
                message="Require min_event_time < 0 <= max_event_time and a "
                "connected window including the normalization period -1.",
            )
        )
        return [], excluded, failures, {}

    # object_id -> period -> row ; also flag duplicate object-period
    rows: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for obs in observations:
        rows[obs.object_id][obs.period].append(obs)

    first_treat: dict[str, int] = {}
    never_treated: list[str] = []
    all_periods: set[int] = set()

    for oid, by_period in rows.items():
        dup = [p for p, rs in by_period.items() if len(rs) > 1]
        if dup:
            excluded.append(
                ExcludedRecord(
                    object_id=oid,
                    reasons=[FailureCategory.DUPLICATE_OBJECT_PERIOD],
                    periods_present=sorted(by_period),
                    detail={"duplicate_periods": dup},
                )
            )
            failures.append(
                Failure(
                    category=FailureCategory.DUPLICATE_OBJECT_PERIOD,
                    severity=Severity.ERROR,
                    message=f"Object {oid} has duplicate rows in periods {dup}.",
                    object_ids=[oid],
                )
            )
            continue

        treated_periods = sorted(
            p for p, rs in by_period.items() if rs[0].treated_this_period
        )
        label = next(
            (rs[0].treated_group for rs in by_period.values() if rs[0].treated_group is not None),
            None,
        )
        all_periods.update(by_period.keys())

        if treated_periods:
            first_treat[oid] = treated_periods[0]
        elif label is True:
            # Labelled treated but never observed under treatment: cohort unknown.
            failures.append(
                Failure(
                    category=FailureCategory.EVENT_STAGGER_UNSUPPORTED,
                    severity=Severity.ERROR,
                    message=f"Object {oid} is labelled treated but is never "
                    "observed as treated; its cohort cannot be determined, "
                    "which is outside the supported single-cohort model.",
                    object_ids=[oid],
                )
            )
        else:
            # Control: if the label ever says treated_group=True it would have
            # been caught above; here it is a never-treated control.
            if any(
                rs[0].treated_group is True for rs in by_period.values()
            ):
                continue
            never_treated.append(oid)

    # Contaminated controls: labelled control yet treated at some point.
    for oid in list(never_treated):
        if any(
            rs[0].treated_group is False and rs[0].treated_this_period
            for rs in rows[oid].values()
        ):
            excluded.append(
                ExcludedRecord(
                    object_id=oid,
                    reasons=[FailureCategory.CONTAMINATED_CONTROL],
                    periods_present=sorted(rows[oid]),
                    detail={"note": "control object treated in some period"},
                )
            )
            failures.append(
                Failure(
                    category=FailureCategory.CONTAMINATED_CONTROL,
                    severity=Severity.ERROR,
                    message=f"Control object {oid} is treated in some period "
                    "(contaminated control); excluded from the event study.",
                    object_ids=[oid],
                )
            )
            never_treated.remove(oid)

    # ---- Support test: exactly one common cohort -------------------------- #
    cohorts = set(first_treat.values())
    if len(cohorts) > 1:
        offenders = sorted(first_treat, key=lambda o: (first_treat[o], o))
        failures.append(
            Failure(
                category=FailureCategory.EVENT_STAGGER_UNSUPPORTED,
                severity=Severity.ERROR,
                message="Staggered treatment timing detected: treated objects "
                f"are first treated in different periods {sorted(cohorts)}. "
                "This is outside the supported single-cohort event-study model "
                "and is REFUSED (naive TWFE event coefficients use forbidden "
                "comparisons under staggered adoption).",
                object_ids=offenders,
                detail={"cohorts": {o: first_treat[o] for o in offenders}},
            )
        )
        return [], excluded, failures, {}

    if not first_treat:
        failures.append(
            Failure(
                category=FailureCategory.DEGENERATE_DESIGN,
                severity=Severity.ERROR,
                message="No treated object with an observed treatment period.",
            )
        )
        return [], excluded, failures, {}

    if not never_treated:
        failures.append(
            Failure(
                category=FailureCategory.DEGENERATE_DESIGN,
                severity=Severity.ERROR,
                message="No never-treated controls; event-study DID needs a "
                "control counterfactual.",
            )
        )
        return [], excluded, failures, {}

    cohort_period = next(iter(cohorts))
    ref_period = cohort_period - 1
    points: list[EventTimePoint] = []
    missing_seen: set[str] = set()

    def pair(oid: str, target_period: int):
        by_period = rows[oid]
        if ref_period in by_period and target_period in by_period:
            return (
                float(by_period[target_period][0].y),
                float(by_period[ref_period][0].y),
            )
        return None

    for k in range(min_event_time, max_event_time + 1):
        target = cohort_period + k

        if k == -1:
            points.append(
                EventTimePoint(
                    event_time=k,
                    n_treated_objects=len(first_treat),
                    n_control_objects=len(never_treated),
                    estimate=0.0,
                    standard_error=None,
                    supported=True,
                    note="Normalization period: coefficient fixed at 0 by "
                    "construction.",
                )
            )
            continue

        t_pairs, c_pairs = [], []
        for oid in first_treat:
            pr = pair(oid, target)
            if pr is None:
                if oid not in missing_seen:
                    missing_seen.add(oid)
                    present = sorted(rows[oid])
                    excluded.append(
                        ExcludedRecord(
                            object_id=oid,
                            reasons=[FailureCategory.IDENTITY_MISSING_PERIOD],
                            periods_present=present,
                            detail={
                                "required_periods": sorted({ref_period, target}),
                                "event_time": k,
                            },
                        )
                    )
                continue
            t_pairs.append(pr)
        for oid in never_treated:
            pr = pair(oid, target)
            if pr is None:
                if f"ctrl:{oid}" not in missing_seen:
                    missing_seen.add(f"ctrl:{oid}")
                    excluded.append(
                        ExcludedRecord(
                            object_id=oid,
                            reasons=[FailureCategory.IDENTITY_MISSING_PERIOD],
                            periods_present=sorted(rows[oid]),
                            detail={
                                "required_periods": sorted({ref_period, target}),
                                "event_time": k,
                            },
                        )
                    )
                continue
            c_pairs.append(pr)

        if target not in all_periods:
            points.append(
                EventTimePoint(
                    event_time=k,
                    n_treated_objects=len(t_pairs),
                    n_control_objects=len(c_pairs),
                    estimate=None,
                    standard_error=None,
                    supported=False,
                    note=f"Calendar period {target} is absent from the data.",
                )
            )
            continue

        if len(t_pairs) < 1 or len(c_pairs) < 1:
            points.append(
                EventTimePoint(
                    event_time=k,
                    n_treated_objects=len(t_pairs),
                    n_control_objects=len(c_pairs),
                    estimate=None,
                    standard_error=None,
                    supported=False,
                    note="Insufficient objects observed in both target and "
                    "normalization period (missing period is not zero).",
                )
            )
            continue

        est, se, df, perfect_fit = _point_estimate(t_pairs, c_pairs)
        few_clusters = len(t_pairs) < 2 or len(c_pairs) < 2
        if few_clusters:
            points.append(
                EventTimePoint(
                    event_time=k,
                    n_treated_objects=len(t_pairs),
                    n_control_objects=len(c_pairs),
                    estimate=est,
                    standard_error=None,
                    supported=True,
                    note="Point shown but clustered SE not identified (need "
                    ">=2 objects per group); treat as descriptive only.",
                )
            )
        elif perfect_fit:
            points.append(
                EventTimePoint(
                    event_time=k,
                    n_treated_objects=len(t_pairs),
                    n_control_objects=len(c_pairs),
                    estimate=est,
                    standard_error=0.0,
                    supported=True,
                    note="DID vs event-time -1; within-group residual variation "
                    f"is exactly zero (perfect synthetic fit), so the CR1 "
                    f"clustered SE is 0 by construction, df={df}.",
                )
            )
        else:
            points.append(
                EventTimePoint(
                    event_time=k,
                    n_treated_objects=len(t_pairs),
                    n_control_objects=len(c_pairs),
                    estimate=est,
                    standard_error=se,
                    supported=True,
                    note=f"DID vs event-time -1; object-clustered CR1 SE, df={df}.",
                )
            )

    summary = {
        "cohort_period": cohort_period,
        "normalization_period": ref_period,
        "n_treated_objects": len(first_treat),
        "n_control_objects": len(never_treated),
        "supported_model": "single_common_cohort_did_event_study",
    }
    return points, excluded, failures, summary
