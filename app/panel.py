"""Panel alignment: object identity across periods.

Key rules (enforced, not assumed away):
* Identity is the *object_id*, aligned across periods. A missing period is a
  missing observation for that object -- it is NEVER imputed as zero.
* The estimation sample is *balanced*: an object enters only if it is observed
  in both periods. Weights are *fixed per object* (one weight, identical in
  both periods), never reweighted separately by period.
* Duplicate (object, period) rows are ambiguous and excluded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Optional

from .contracts import ExcludedRecord, Failure, FailureCategory, Severity


@dataclass(frozen=True)
class AlignedObject:
    object_id: str
    group: str                 # "treated" | "control"
    pre_y: float
    post_y: float
    pre_was_treated: bool
    post_was_treated: bool
    weight: float              # fixed, identical in both periods

    @property
    def delta(self) -> float:
        return self.post_y - self.pre_y


@dataclass
class PanelBuildResult:
    aligned: dict[str, AlignedObject] = field(default_factory=dict)
    excluded: list[ExcludedRecord] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)

    def by_group(self, group: str) -> list[AlignedObject]:
        return [o for o in self.aligned.values() if o.group == group]


def _fail(
    bucket: list[Failure],
    category: FailureCategory,
    severity: Severity,
    message: str,
    object_ids: Optional[list[str]] = None,
    detail: Optional[dict] = None,
) -> None:
    bucket.append(
        Failure(
            category=category,
            severity=severity,
            message=message,
            object_ids=object_ids or [],
            detail=detail or {},
        )
    )


def build_balanced_panel(
    observations,
    pre_period: int,
    post_period: int,
) -> PanelBuildResult:
    """Align raw observations into a fixed-weight balanced two-period panel.

    ``observations`` are pydantic Observation objects (duck-typed attributes).
    Only rows whose period is ``pre_period`` or ``post_period`` are considered.
    """
    result = PanelBuildResult()

    if pre_period == post_period:
        _fail(
            result.failures,
            FailureCategory.DEGENERATE_DESIGN,
            Severity.ERROR,
            f"pre_period and post_period must differ, both are {pre_period}.",
        )
        return result

    # object_id -> period -> list[row]
    rows: dict[str, dict[int, list]] = {}
    periods_seen: set[int] = set()
    for obs in observations:
        periods_seen.add(obs.period)
        if obs.period not in (pre_period, post_period):
            continue
        if not isfinite(obs.y):
            _fail(
                result.failures,
                FailureCategory.INVALID_REQUEST,
                Severity.ERROR,
                f"Non-finite outcome for object {obs.object_id} in period "
                f"{obs.period}.",
                object_ids=[obs.object_id],
            )
            return result
        rows.setdefault(obs.object_id, {}).setdefault(obs.period, []).append(obs)

    if pre_period not in periods_seen or post_period not in periods_seen:
        _fail(
            result.failures,
            FailureCategory.DEGENERATE_DESIGN,
            Severity.ERROR,
            f"Data must contain both periods {pre_period} and {post_period}.",
        )
        return result

    for oid, by_period in rows.items():
        present = sorted(by_period.keys())

        # Duplicate rows for the same object-period: identity ambiguous.
        dup_periods = [p for p, rs in by_period.items() if len(rs) > 1]
        if dup_periods:
            result.excluded.append(
                ExcludedRecord(
                    object_id=oid,
                    reasons=[FailureCategory.DUPLICATE_OBJECT_PERIOD],
                    periods_present=present,
                    detail={"duplicate_periods": dup_periods},
                )
            )
            _fail(
                result.failures,
                FailureCategory.DUPLICATE_OBJECT_PERIOD,
                Severity.ERROR,
                f"Object {oid} has multiple rows in period(s) {dup_periods}; "
                "identity ambiguous, excluded.",
                object_ids=[oid],
            )
            continue

        # Missing period -> excluded. Explicitly NOT treated as zero.
        if pre_period not in by_period or post_period not in by_period:
            missing = [p for p in (pre_period, post_period) if p not in by_period]
            result.excluded.append(
                ExcludedRecord(
                    object_id=oid,
                    reasons=[FailureCategory.IDENTITY_MISSING_PERIOD],
                    periods_present=present,
                    detail={"missing_periods": missing},
                )
            )
            _fail(
                result.failures,
                FailureCategory.IDENTITY_MISSING_PERIOD,
                Severity.WARNING,
                f"Object {oid} is missing period(s) {missing}; a missing period "
                "is not zero. Excluded from the balanced sample.",
                object_ids=[oid],
            )
            continue

        pre_row = by_period[pre_period][0]
        post_row = by_period[post_period][0]

        group = _resolve_group(oid, pre_row, post_row, result)
        if group is None:
            # failure already recorded; object excluded
            result.excluded.append(
                ExcludedRecord(
                    object_id=oid,
                    reasons=[FailureCategory.INVALID_REQUEST],
                    periods_present=present,
                    detail={"reason": "ambiguous_group_membership"},
                )
            )
            continue

        # Fixed weight: one per object, identical in both periods.
        result.aligned[oid] = AlignedObject(
            object_id=oid,
            group=group,
            pre_y=float(pre_row.y),
            post_y=float(post_row.y),
            pre_was_treated=bool(pre_row.treated_this_period),
            post_was_treated=bool(post_row.treated_this_period),
            weight=1.0,
        )

    return result


def _resolve_group(oid: str, pre_row, post_row, result: PanelBuildResult) -> Optional[str]:
    labels = [r.treated_group for r in (pre_row, post_row) if r.treated_group is not None]
    if labels:
        if any(bool(x) != bool(labels[0]) for x in labels):
            _fail(
                result.failures,
                FailureCategory.INVALID_REQUEST,
                Severity.ERROR,
                f"Object {oid} has conflicting treated_group labels across periods.",
                object_ids=[oid],
            )
            return None
        is_treated_group = bool(labels[0])
    else:
        # No explicit label: treated group iff it is ever treated.
        is_treated_group = bool(pre_row.treated_this_period or post_row.treated_this_period)

    return "treated" if is_treated_group else "control"
