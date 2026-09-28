"""Estimation kernels.

Three estimators, each deliberately small and independently testable:

* :func:`four_cell_decomposition` -- the hand-checkable 2x2 means. This is the
  primary DID point estimate and is exactly reproducible on paper.
* :func:`did_first_difference_regression` -- the *same* estimand reached by OLS:
  Δy_i = a + b·treated_i + e with weights fixed at baseline and standard
  errors clustered on the unit. ``b`` must equal the four-cell DID to machine
  precision; the tests assert this agreement.
* :func:`event_study_twfe` -- event-time regression with unit and period fixed
  effects, the -1 lead normalized to zero, clustered on the unit, with an
  explicit support check (staggered treatment beyond support is refused).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.contracts.models import (
    ControlGroup,
    FailureCategory,
    WeightPolicy,
)
from app.core.alignment import AlignedPanel, UnitRecord
from app.core.errors import EstimationError
from app.core.inference import OLSResult, weighted_ols_cluster


# --------------------------------------------------------------------------- #
# Weighted helpers
# --------------------------------------------------------------------------- #
def _weighted_mean(values: list[float], weights: list[float]) -> float:
    w = np.asarray(weights, dtype=float)
    v = np.asarray(values, dtype=float)
    total = float(np.sum(w * v))
    denom = float(np.sum(w))
    if denom <= 0:
        raise EstimationError(FailureCategory.INVALID_WEIGHT, "zero total weight in a 2x2 cell")
    return total / denom


def _unit_weight(panel: AlignedPanel, uid: str, period: int) -> float:
    if panel.weight_policy is WeightPolicy.NONE:
        return 1.0
    if panel.weight_policy is WeightPolicy.UNIT_FIXED:
        return panel.units[uid].fixed_weight
    return panel.units[uid].raw_weight[period]


# --------------------------------------------------------------------------- #
# 2x2 DID
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FourCell:
    treat_pre: float
    treat_post: float
    control_pre: float
    control_post: float
    treat_change: float
    control_change: float
    did: float
    n_treat: int
    n_control: int
    total_weight_treat: float
    total_weight_control: float


def four_cell_decomposition(
    panel: AlignedPanel,
    treated: list[str],
    control: list[str],
    *,
    n_contaminated: int = 0,
) -> FourCell:
    if not treated:
        raise EstimationError(FailureCategory.SINGLE_GROUP, "no treated units survive alignment")
    if not control:
        if n_contaminated:
            raise EstimationError(
                FailureCategory.NO_VALID_CONTROL_GROUP,
                f"no clean control units survive alignment ({n_contaminated} control unit(s) "
                "excluded as contaminated; missing units are excluded, not zeroed)",
            )
        raise EstimationError(
            FailureCategory.SINGLE_GROUP,
            "only one group (treated) survives alignment; DID needs a comparison group",
        )
    pre, post = panel.pre_period, panel.post_period

    def cell(uids: list[str], period: int) -> tuple[float, list[float], list[float]]:
        vals = [panel.units[u].y[period] for u in uids]
        wts = [_unit_weight(panel, u, period) for u in uids]
        return _weighted_mean(vals, wts), vals, wts

    tp, _, wtp = cell(treated, pre)
    tpost, _, wtpost = cell(treated, post)
    cp, _, wcp = cell(control, pre)
    cpost, _, wcpost = cell(control, post)

    # Unit-fixed weights are, by construction, identical in both periods, so
    # the weighted mean of within-unit changes equals the difference of the
    # weighted cell means. Under every policy the *baseline* weight is reused
    # for the change (weights are frozen before estimation, never re-chosen).
    change_w_t = [_unit_weight(panel, u, pre) for u in treated]
    change_w_c = [_unit_weight(panel, u, pre) for u in control]
    treat_change = _weighted_mean(
        [panel.units[u].y[post] - panel.units[u].y[pre] for u in treated],
        change_w_t,
    )
    control_change = _weighted_mean(
        [panel.units[u].y[post] - panel.units[u].y[pre] for u in control],
        change_w_c,
    )

    return FourCell(
        treat_pre=tp,
        treat_post=tpost,
        control_pre=cp,
        control_post=cpost,
        treat_change=treat_change,
        control_change=control_change,
        did=treat_change - control_change,
        n_treat=len(treated),
        n_control=len(control),
        total_weight_treat=float(sum(wtp)),
        total_weight_control=float(sum(wcp)),
    )


def did_first_difference_regression(
    panel: AlignedPanel,
    treated: list[str],
    control: list[str],
    *,
    cluster_adjustment: str = "crv1",
) -> tuple[OLSResult, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Δy_i = a + b·T_i + e, baseline-fixed weights, SE clustered on unit.

    Returns the OLS result together with the assembled first-difference arrays
    (dy, treatment indicator, weights, cluster ids) so callers/tests can audit
    exactly what entered the regression.
    """
    uids = treated + control
    treatment = np.array([1.0] * len(treated) + [0.0] * len(control))
    dy = np.array([panel.units[u].y[panel.post_period] - panel.units[u].y[panel.pre_period] for u in uids])
    weights = np.array([_unit_weight(panel, u, panel.pre_period) for u in uids])
    clusters = np.array(uids)
    x = np.column_stack([np.ones(len(uids)), treatment])
    ols = weighted_ols_cluster(
        x,
        dy,
        weights,
        clusters,
        cluster_adjustment=cluster_adjustment,
        coef_names=["intercept", "treated_x_post"],
    )
    return ols, dy, treatment, weights, clusters


# --------------------------------------------------------------------------- #
# Event-time / staggered DID
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EventSupport:
    window: list[int]                 # sorted event times in the design
    cohorts: dict[int, list[str]]     # first-treatment period -> unit ids
    never_treated: list[str]
    not_yet_treated: list[str]
    supported_k: set[int]             # event times with >=1 treated observation


def build_event_support(
    units: dict[str, UnitRecord],
    *,
    control_group: ControlGroup,
    requested_min: int | None,
    requested_max: int | None,
) -> EventSupport:
    cohorts: dict[int, list[str]] = {}
    never: list[str] = []
    for uid, rec in units.items():
        g = rec.first_treat()
        if g is None:
            never.append(uid)
        else:
            cohorts.setdefault(g, []).append(uid)
    for uids in cohorts.values():
        uids.sort()
    never.sort()

    if not cohorts:
        raise EstimationError(
            FailureCategory.SINGLE_GROUP,
            "event study needs at least one treated cohort (a unit that switches on)",
        )

    # Treated support: event time k is supported if some treated unit is
    # observed at calendar period g + k.
    supported: set[int] = set()
    for g, uids in cohorts.items():
        for uid in uids:
            for t in units[uid].y:
                supported.add(t - g)

    global_min = min(rec_period for rec in units.values() for rec_period in rec.y)
    global_max = max(rec_period for rec in units.values() for rec_period in rec.y)

    if requested_min is None:
        requested_min = min(supported)
    if requested_max is None:
        requested_max = max(supported)
    if requested_min > requested_max:
        raise EstimationError(
            FailureCategory.INVALID_REQUEST,
            f"min_event_time ({requested_min}) > max_event_time ({requested_max})",
        )

    window = list(range(requested_min, requested_max + 1))
    # -1 is the normalized reference; every OTHER requested k must be in the
    # observed support, otherwise the staggered design is asked to look where
    # no cohort has data -- an explicit refusal, never an extrapolation.
    out_of_support = [k for k in window if k != -1 and k not in supported]
    if out_of_support:
        raise EstimationError(
            FailureCategory.OUT_OF_SUPPORT_EVENT_TIME,
            f"event time(s) {out_of_support} outside observed support {sorted(supported)} "
            f"(calendar span {global_min}..{global_max}); refusing to extrapolate",
        )

    # not-yet-treated units: treated cohorts can serve as controls only for
    # calendar periods strictly before their own onset.
    not_yet: list[str] = []
    if control_group is ControlGroup.NOT_YET_TREATED:
        not_yet = [u for uids in cohorts.values() for u in uids]

    if control_group is ControlGroup.NEVER_TREATED and not never:
        raise EstimationError(
            FailureCategory.NO_VALID_CONTROL_GROUP,
            "strict event study needs never-treated controls; none exist and not-yet-treated "
            "comparison was not selected",
        )

    return EventSupport(
        window=window,
        cohorts=dict(sorted(cohorts.items())),
        never_treated=never,
        not_yet_treated=sorted(not_yet),
        supported_k=supported,
    )


def event_study_twfe(
    units: dict[str, UnitRecord],
    support: EventSupport,
    *,
    weight_policy: WeightPolicy,
    cluster_adjustment: str = "crv1",
):
    """y_it = alpha_i + gamma_t + sum_{k != -1} beta_k·D_it(k) + e.

    Estimated with explicit unit/period dummies (data are small), baseline-fixed
    unit weights, and unit-clustered standard errors. Returns assembled arrays
    and metadata for audit. The -1 lead is the omitted reference category.
    """
    window = support.window
    event_cols = [k for k in window if k != -1]

    treated_uids = sorted(u for uids in support.cohorts.values() for u in uids)
    control_uids = support.never_treated if support.never_treated else support.not_yet_treated
    all_uids = treated_uids + control_uids

    all_periods = sorted({t for uid in all_uids for t in units[uid].y})
    unit_index = {uid: j for j, uid in enumerate(all_uids)}
    period_index = {t: j for j, t in enumerate(all_periods)}

    rows: list[tuple[str, int, float, float, int | None]] = []
    # Treated rows: retain only observations whose event time is in the window
    # (the clean comparison set).
    for uid in treated_uids:
        rec = units[uid]
        g = rec.first_treat()
        for t in sorted(rec.y):
            k = t - g
            if k in window:
                w = rec.fixed_weight if weight_policy is WeightPolicy.UNIT_FIXED else (
                    1.0 if weight_policy is WeightPolicy.NONE else rec.raw_weight[t]
                )
                rows.append((uid, t, rec.y[t], w, k))
    # Control rows.
    for uid in control_uids:
        rec = units[uid]
        is_not_yet = uid in set(support.not_yet_treated) and uid not in set(support.never_treated)
        for t in sorted(rec.y):
            if is_not_yet and rec.treated[t]:
                # A not-yet-treated control stops being a control at onset.
                continue
            w = rec.fixed_weight if weight_policy is WeightPolicy.UNIT_FIXED else (
                1.0 if weight_policy is WeightPolicy.NONE else rec.raw_weight[t]
            )
            rows.append((uid, t, rec.y[t], w, None))

    if not rows:
        raise EstimationError(FailureCategory.NO_ESTIMABLE_UNITS, "no rows enter the event-study design")

    n_event = len(event_cols)
    n_units = len(all_uids)
    n_periods = len(all_periods)
    # Reference dummies: first unit and first period dropped.
    n_cols = n_event + (n_units - 1) + (n_periods - 1)
    x = np.zeros((len(rows), n_cols))
    y = np.zeros(len(rows))
    w = np.zeros(len(rows))
    clusters = np.empty(len(rows), dtype=object)

    event_pos = {k: j for j, k in enumerate(event_cols)}
    unit_dummy_start = n_event
    period_dummy_start = n_event + (n_units - 1)

    observed_k: set[int] = set()
    for r, (uid, t, val, wt, k) in enumerate(rows):
        y[r] = val
        w[r] = wt
        clusters[r] = uid
        if k is not None and k != -1:
            x[r, event_pos[k]] = 1.0
            observed_k.add(k)
        j = unit_index[uid]
        if j > 0:
            x[r, unit_dummy_start + (j - 1)] = 1.0
        q = period_index[t]
        if q > 0:
            x[r, period_dummy_start + (q - 1)] = 1.0

    missing_event = [k for k in event_cols if k not in observed_k]
    if missing_event:  # defensive: support check should have caught this
        raise EstimationError(
            FailureCategory.OUT_OF_SUPPORT_EVENT_TIME,
            f"event time(s) {missing_event} have no treated rows in the design",
        )

    coef_names = [f"event_time_{k}" for k in event_cols]
    ols = weighted_ols_cluster(
        x, y, w, clusters, cluster_adjustment=cluster_adjustment, coef_names=coef_names
    )
    return ols, event_cols, rows, all_uids, all_periods
