"""File-access baselines with z-score anomaly detection.

A baseline covers the 14 *complete* calendar days immediately before the
assessed day, counted in the employee's organization timezone. Days with zero
activity count as 0; eligibility instead requires that the employee's history
stretches back to cover the whole window. Every recomputation inserts a new,
immutable version row — old versions, alerts and investigation records are
never overwritten.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import fmean, pstdev

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    AlertRule,
    Baseline,
    BaselineStatus,
    Event,
    Organization,
    User,
)
from .engine import insert_alert_if_absent
from .timeutil import BASELINE_EVENT_TYPES, user_timezone

settings = get_settings()
METRIC = "file_access_daily"


def _tz_for(db: Session, user: User) -> str:
    if user.org_id is not None:
        org = db.get(Organization, user.org_id)
        if org is not None:
            return org.timezone
    return "UTC"


def _local_day_counts(db: Session, user_id: int, tz_name: str, lo: date, hi: date) -> dict[str, int]:
    """Counts bucketed by local calendar date, for days in [lo, hi]."""
    local_day = func.date(Event.occurred_at.op("AT TIME ZONE")(tz_name))
    rows = db.execute(
        select(local_day, func.count())
        .where(
            Event.user_id == user_id,
            Event.type.in_([t for t in BASELINE_EVENT_TYPES]),
            Event.occurred_at >= _local_day_start_utc(lo, tz_name),
            Event.occurred_at < _local_day_start_utc(hi + timedelta(days=1), tz_name),
        )
        .group_by(local_day)
    ).all()
    return {str(r[0]): int(r[1]) for r in rows}


def _local_day_start_utc(day: date, tz_name: str) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=user_timezone(tz_name))


def compute_baseline(db: Session, user_id: int, target_day: date | None = None) -> tuple[Baseline, int | None]:
    """Compute and persist a new baseline version. Returns (baseline, alert_id)."""
    user = db.get(User, user_id)
    if user is None:
        raise ValueError(f"unknown user {user_id}")

    tz_name = _tz_for(db, user)
    tz = user_timezone(tz_name)
    if target_day is None:
        # Default assessed day: yesterday (the latest complete local day).
        target_day = (datetime.now(tz) - timedelta(days=1)).date()

    window_lo = target_day - timedelta(days=settings.baseline_days)
    window_hi = target_day - timedelta(days=1)

    counts = _local_day_counts(db, user_id, tz_name, window_lo, target_day)
    window_counts = {
        str(window_lo + timedelta(days=i)): counts.get(
            str(window_lo + timedelta(days=i)), 0
        )
        for i in range(settings.baseline_days)
    }
    observed = counts.get(str(target_day), 0)

    # Eligibility: a full 14-day span must exist before the assessed day.
    earliest = db.scalar(
        select(func.min(Event.occurred_at)).where(
            Event.user_id == user_id,
            Event.type.in_([t for t in BASELINE_EVENT_TYPES]),
        )
    )
    earliest_local_date = earliest.astimezone(tz).date() if earliest is not None else None
    has_full_window = earliest_local_date is not None and earliest_local_date <= window_lo

    values = [window_counts[d] for d in sorted(window_counts)]
    days_used = settings.baseline_days if has_full_window else 0
    if not has_full_window:
        if earliest_local_date is not None:
            days_used = max(0, (window_hi - earliest_local_date).days + 1)
        status = BaselineStatus.insufficient_history
        mean = stddev = zscore = None
    else:
        mean = fmean(values)
        stddev = pstdev(values)
        if stddev == 0.0:
            status = BaselineStatus.zero_variance
            zscore = None
        else:
            status = BaselineStatus.ok
            zscore = (observed - mean) / stddev

    last_version = db.scalar(
        select(func.coalesce(func.max(Baseline.version), 0)).where(
            Baseline.user_id == user_id, Baseline.metric == METRIC
        )
    )
    baseline = Baseline(
        user_id=user_id,
        metric=METRIC,
        version=int(last_version) + 1,
        target_date=datetime(target_day.year, target_day.month, target_day.day),
        status=status,
        days_used=days_used,
        mean=mean,
        stddev=stddev,
        daily_counts=window_counts,
        observed_count=observed,
        zscore=zscore,
    )
    db.add(baseline)
    db.flush()

    alert_id = None
    if status is BaselineStatus.ok and zscore is not None and zscore >= settings.baseline_zscore:
        alert_id = insert_alert_if_absent(
            db,
            rule=AlertRule.file_access_baseline,
            # One alert per assessed day; versions preserve the triggering evidence.
            dedup_key=f"baseline:{user_id}:{target_day.isoformat()}",
            title=f"File-access volume anomaly on {target_day.isoformat()} (z={zscore:.2f})",
            user_id=user_id,
            severity="high",
            window_start=_local_day_start_utc(target_day, tz_name),
            window_end=_local_day_start_utc(target_day + timedelta(days=1), tz_name),
            baseline_id=baseline.id,
            evidence={
                "baseline_version": baseline.version,
                "target_day": target_day.isoformat(),
                "timezone": tz_name,
                "observed_count": observed,
                "mean": mean,
                "stddev": stddev,
                "zscore": zscore,
                "threshold_z": settings.baseline_zscore,
                "window_days": settings.baseline_days,
                "daily_counts": window_counts,
            },
        )
    db.flush()
    return baseline, alert_id


def latest_baseline(db: Session, user_id: int) -> Baseline | None:
    return db.scalars(
        select(Baseline)
        .where(Baseline.user_id == user_id)
        .order_by(Baseline.version.desc())
        .limit(1)
    ).first()
