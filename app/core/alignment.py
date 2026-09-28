"""Object-identity alignment across periods.

This is where the panel's *state discipline* lives. The principles enforced
here are the ones the brief calls out explicitly:

1. A unit missing a period is a distinct state, never an implicit zero. There
   is no ``y = 0`` imputation anywhere in the pipeline; such a unit is aligned
   by identity and then either excluded (balanced strategy, with a record) or
   handled period-by-period (unbalanced strategy).
2. Balance strategy and weights are fixed *before* estimation. Under
   ``unit_fixed`` a unit's weight is frozen at its baseline value and reused at
   every period so a first difference is never re-weighted mid-run.
3. Treatment is an explicit state, so reversal, always-treated and control
   contamination are detectable rather than silently absorbed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.contracts.models import (
    BalanceStrategy,
    ExcludedRecord,
    FailureCategory,
    Observation,
    StepTrace,
    WeightPolicy,
)
from app.core.errors import EstimationError


@dataclass
class UnitRecord:
    unit_id: str
    y: dict[int, float] = field(default_factory=dict)
    treated: dict[int, bool] = field(default_factory=dict)
    raw_weight: dict[int, float] = field(default_factory=dict)
    fixed_weight: float = 1.0

    def periods(self) -> list[int]:
        return sorted(self.y.keys())

    def first_treat(self) -> int | None:
        treated_periods = [t for t, on in self.treated.items() if on]
        return min(treated_periods) if treated_periods else None

    def ever_treated(self) -> bool:
        return any(self.treated.values())


@dataclass
class AlignedPanel:
    units: dict[str, UnitRecord]
    all_periods: list[int]
    pre_period: int
    post_period: int
    balance: BalanceStrategy
    weight_policy: WeightPolicy
    # Units observed at *both* selected periods: the identity-aligned sample.
    balanced_units: list[str]
    excluded: list[ExcludedRecord]
    steps: list[StepTrace]

    def weight(self, unit_id: str, period: int) -> float:
        rec = self.units[unit_id]
        if self.weight_policy is WeightPolicy.NONE:
            return 1.0
        if self.weight_policy is WeightPolicy.UNIT_FIXED:
            return rec.fixed_weight
        return rec.raw_weight.get(period, 1.0)


def _step(step: str, detail: str, n_in: int, n_out: int) -> StepTrace:
    return StepTrace(step=step, detail=detail, n_units_in=n_in, n_units_out=n_out)


def align_panel(
    observations: list[Observation],
    *,
    balance: BalanceStrategy,
    weight_policy: WeightPolicy,
    pre_period: int | None = None,
    post_period: int | None = None,
) -> AlignedPanel:
    """Validate, de-duplicate and align observations by unit identity.

    Raises :class:`EstimationError` for unrecoverable structural problems;
    per-unit problems (missing periods, bad treatment paths) are collected as
    :class:`ExcludedRecord` and the unit is dropped from the estimable set.
    """
    steps: list[StepTrace] = []
    excluded: list[ExcludedRecord] = []

    if not observations:
        raise EstimationError(FailureCategory.EMPTY_PANEL, "request contained no observations")

    # --- pass 1: index by identity, reject duplicate (unit, period) cells ---
    units: dict[str, UnitRecord] = {}
    seen: set[tuple[str, int]] = set()
    for obs in observations:
        key = (obs.unit_id, obs.period)
        if key in seen:
            raise EstimationError(
                FailureCategory.DUPLICATE_UNIT_PERIOD,
                f"duplicate cell for unit={obs.unit_id!r} period={obs.period}",
            )
        seen.add(key)
        rec = units.get(obs.unit_id)
        if rec is None:
            rec = UnitRecord(unit_id=obs.unit_id)
            units[obs.unit_id] = rec
        rec.y[obs.period] = obs.y
        rec.treated[obs.period] = bool(obs.treated)
        rec.raw_weight[obs.period] = obs.weight

    n_raw = len(units)
    steps.append(_step("ingest", f"indexed {len(observations)} cells into {n_raw} unique units", len(observations), n_raw))

    all_periods = sorted({obs.period for obs in observations})
    if len(all_periods) < 2:
        raise EstimationError(
            FailureCategory.SINGLE_PERIOD,
            f"DID needs >= 2 periods; panel contains only {all_periods}",
            steps=steps,
        )

    pre = pre_period if pre_period is not None else all_periods[0]
    post = post_period if post_period is not None else all_periods[-1]
    if pre >= post:
        raise EstimationError(
            FailureCategory.INVALID_REQUEST,
            f"pre_period ({pre}) must be strictly before post_period ({post})",
            steps=steps,
        )
    if pre not in all_periods or post not in all_periods:
        raise EstimationError(
            FailureCategory.NOT_TWO_PERIODS,
            f"requested periods ({pre},{post}) are not both observed; observed={all_periods}",
            steps=steps,
        )
    steps.append(_step("period_select", f"pre={pre} post={post}; observed periods={all_periods}", n_raw, n_raw))

    # --- freeze unit weights at the baseline period before any dropping ----
    for rec in units.values():
        baseline = pre if pre in rec.raw_weight else min(rec.raw_weight)
        rec.fixed_weight = rec.raw_weight[baseline]
        if rec.fixed_weight <= 0:
            excluded.append(
                ExcludedRecord(
                    unit_id=rec.unit_id,
                    reason=FailureCategory.INVALID_WEIGHT,
                    detail=f"baseline weight {rec.fixed_weight} is not positive",
                    periods=rec.periods(),
                )
            )

    # --- identity alignment: a missing period is a recorded exclusion ------
    balanced: list[str] = []
    for uid, rec in units.items():
        if any(x.reason is FailureCategory.INVALID_WEIGHT and x.unit_id == uid for x in excluded):
            continue
        missing = [t for t in (pre, post) if t not in rec.y]
        if missing:
            excluded.append(
                ExcludedRecord(
                    unit_id=uid,
                    reason=FailureCategory.UNBALANCED_PANEL,
                    detail=(
                        f"unit observed at {rec.periods()} but missing period(s) {missing}; "
                        "missing periods are NOT imputed as zero"
                    ),
                    periods=rec.periods(),
                )
            )
            continue
        balanced.append(uid)

    steps.append(
        _step(
            "identity_align",
            f"aligned units observed at both {pre} and {post}; "
            f"{len(excluded)} units excluded (missing periods carry no zero imputation)",
            n_raw,
            len(balanced),
        )
    )

    if not balanced:
        raise EstimationError(
            FailureCategory.NO_ESTIMABLE_UNITS,
            "no unit is observed at both selected periods under the balanced strategy",
            excluded=excluded,
            steps=steps,
        )

    return AlignedPanel(
        units=units,
        all_periods=all_periods,
        pre_period=pre,
        post_period=post,
        balance=balance,
        weight_policy=weight_policy,
        balanced_units=sorted(balanced),
        excluded=excluded,
        steps=steps,
    )


def classify_two_by_two(panel: AlignedPanel) -> tuple[list[str], list[str], list[ExcludedRecord], list[StepTrace]]:
    """Split identity-aligned units into treated vs. control for the 2x2.

    Treatment-path discipline:
      * treated pre AND post  -> always-treated, no untreated pre-period, drop
      * untreated pre, treated post -> treated cohort (the ATT population)
      * untreated both -> candidate control
      * treated pre, untreated post -> reversal, violates absorbing treatment
      * candidate control treated in *any* observed period -> contamination
    """
    excluded = list(panel.excluded)
    steps = list(panel.steps)
    treated: list[str] = []
    control: list[str] = []
    pre, post = panel.pre_period, panel.post_period

    for uid in panel.balanced_units:
        rec = panel.units[uid]
        pre_on = rec.treated[pre]
        post_on = rec.treated[post]
        if pre_on and post_on:
            excluded.append(
                ExcludedRecord(
                    unit_id=uid,
                    reason=FailureCategory.INCONSISTENT_TREATMENT_PATH,
                    detail="treated in both periods (always-treated): no untreated pre-period for a 2x2 contrast",
                    periods=[pre, post],
                )
            )
        elif pre_on and not post_on:
            excluded.append(
                ExcludedRecord(
                    unit_id=uid,
                    reason=FailureCategory.INCONSISTENT_TREATMENT_PATH,
                    detail="treatment reverses (treated pre, untreated post): treatment is assumed absorbing",
                    periods=[pre, post],
                )
            )
        elif not pre_on and post_on:
            treated.append(uid)
        else:
            # Candidate control must be untreated in EVERY observed period, not
            # just the two selected ones, else it is contaminated.
            contaminating = [t for t, on in rec.treated.items() if on]
            if contaminating:
                excluded.append(
                    ExcludedRecord(
                        unit_id=uid,
                        reason=FailureCategory.CONTROL_GROUP_CONTAMINATED,
                        detail=f"control unit is treated at period(s) {contaminating}",
                        periods=rec.periods(),
                    )
                )
            else:
                control.append(uid)

    steps.append(
        _step(
            "classify_2x2",
            f"treated={len(treated)} clean controls={len(control)}; "
            f"{sum(1 for e in excluded if e.reason in (FailureCategory.INCONSISTENT_TREATMENT_PATH, FailureCategory.CONTROL_GROUP_CONTAMINATED))} path/contamination exclusions",
            len(panel.balanced_units),
            len(treated) + len(control),
        )
    )
    return sorted(treated), sorted(control), excluded, steps
