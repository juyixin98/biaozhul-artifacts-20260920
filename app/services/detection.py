"""Anomaly detection rules, evaluated synchronously at ingestion time.

All rules key off the event's *occurred_at* (event time), never arrival time,
so late or out-of-order events re-evaluate the windows they affect. Alert
de-duplication relies on the uq_alert_window unique constraint plus a
per-employee row lock taken before detection runs.
"""
from datetime import datetime, timedelta
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Alert, BaselineVersion, Employee, Event

RULE_OFF_HOURS = "off_hours_access"
RULE_DOWNLOAD_BURST = "download_burst"
RULE_USB_FIRST = "usb_first_use"
RULE_BASELINE = "baseline_anomaly"

# Rule parameters
OFF_HOURS_START = 6   # allowed window is [06:00, 22:00) org-local time
OFF_HOURS_END = 22
BURST_WINDOW = timedelta(minutes=10)
BURST_THRESHOLD = 50  # alert when more than 50 downloads in the window
BASELINE_DAYS = 14
Z_THRESHOLD = 3.0


def _create_alert(
    db: Session,
    employee_id: int,
    rule: str,
    window_start: datetime,
    window_end: datetime,
    evidence: dict,
) -> bool:
    exists = (
        db.query(Alert.id)
        .filter(
            Alert.employee_id == employee_id,
            Alert.rule == rule,
            Alert.window_start == window_start,
            Alert.window_end == window_end,
        )
        .first()
    )
    if exists:
        return False
    db.add(
        Alert(
            employee_id=employee_id,
            rule=rule,
            window_start=window_start,
            window_end=window_end,
            status="open",
            version=1,
            evidence=evidence,
        )
    )
    db.flush()
    return True


def _local_day_bounds(local: datetime) -> tuple[datetime, datetime]:
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start, day_start + timedelta(days=1)


def detect_off_hours(db: Session, ev: Event, tz: ZoneInfo) -> int:
    """Access outside [06:00, 22:00) in the employee's org timezone.

    One alert per employee per org-local day.
    """
    local = ev.occurred_at.astimezone(tz)
    if OFF_HOURS_START <= local.hour < OFF_HOURS_END:
        return 0
    day_start, day_end = _local_day_bounds(local)
    evidence = {
        "event_id": ev.event_id,
        "device_id": ev.device_id,
        "local_time": local.isoformat(),
        "timezone": str(tz),
    }
    return 1 if _create_alert(db, ev.employee_id, RULE_OFF_HOURS, day_start, day_end, evidence) else 0


def detect_download_burst(db: Session, ev: Event) -> int:
    """More than 50 downloads in a 10-minute window.

    The window is (t-10min, t]: it includes the current moment and excludes
    the boundary exactly 10 minutes before. A late event at time t affects
    every window ending at a download in [t, t+10min), so all of those are
    re-evaluated; the unique constraint prevents duplicate alerts.
    """
    t = ev.occurred_at
    ends = {t}
    rows = (
        db.query(Event.occurred_at)
        .filter(
            Event.employee_id == ev.employee_id,
            Event.event_type == "file_download",
            Event.occurred_at > t,
            Event.occurred_at <= t + BURST_WINDOW,
        )
        .distinct()
        .all()
    )
    ends.update(r[0] for r in rows)

    created = 0
    for te in ends:
        ws = te - BURST_WINDOW
        cnt = (
            db.query(func.count(Event.id))
            .filter(
                Event.employee_id == ev.employee_id,
                Event.event_type == "file_download",
                Event.occurred_at > ws,
                Event.occurred_at <= te,
            )
            .scalar()
        )
        if cnt > BURST_THRESHOLD:
            if _create_alert(
                db,
                ev.employee_id,
                RULE_DOWNLOAD_BURST,
                ws,
                te,
                {"count": cnt, "threshold": BURST_THRESHOLD, "window_seconds": 600},
            ):
                created += 1
    return created


def detect_usb_first_use(db: Session, ev: Event) -> int:
    """First-ever USB connection observed for a device (by event time)."""
    earlier = (
        db.query(Event.id)
        .filter(
            Event.device_id == ev.device_id,
            Event.event_type == "usb_connect",
            Event.occurred_at < ev.occurred_at,
        )
        .first()
    )
    if earlier:
        return 0
    existing = (
        db.query(Alert.id)
        .filter(
            Alert.employee_id == ev.employee_id,
            Alert.rule == RULE_USB_FIRST,
            Alert.evidence["device_id"].astext == str(ev.device_id),
        )
        .first()
    )
    if existing:
        return 0
    return 1 if _create_alert(
        db,
        ev.employee_id,
        RULE_USB_FIRST,
        ev.occurred_at,
        ev.occurred_at,
        {"device_id": ev.device_id, "event_id": ev.event_id},
    ) else 0


def evaluate_baseline(db: Session, ev: Event, tz: ZoneInfo) -> tuple[str, int]:
    """Z-score of today's file-access count against the previous 14 complete
    org-local days. Always records a new BaselineVersion (never overwrites);
    an existing alert for the day is left untouched, preserving any
    investigation state. Returns (status, alerts_created).
    """
    local = ev.occurred_at.astimezone(tz)
    day_start, day_end = _local_day_bounds(local)
    prev_start = day_start - timedelta(days=BASELINE_DAYS)
    baseline_date = day_start.date()

    today_count = (
        db.query(func.count(Event.id))
        .filter(
            Event.employee_id == ev.employee_id,
            Event.event_type == "file_access",
            Event.occurred_at >= day_start,
            Event.occurred_at < day_end,
        )
        .scalar()
    )

    earliest = (
        db.query(func.min(Event.occurred_at))
        .filter(Event.employee_id == ev.employee_id, Event.event_type == "file_access")
        .scalar()
    )

    counts: list[int] = []
    mu = sd = z = None
    if earliest is None or earliest >= prev_start:
        status = "insufficient_history"
    else:
        rows = (
            db.query(Event.occurred_at)
            .filter(
                Event.employee_id == ev.employee_id,
                Event.event_type == "file_access",
                Event.occurred_at >= prev_start,
                Event.occurred_at < day_start,
            )
            .all()
        )
        per_day = {i: 0 for i in range(BASELINE_DAYS)}
        for (occ,) in rows:
            idx = (occ.astimezone(tz).date() - baseline_date).days + BASELINE_DAYS
            if 0 <= idx < BASELINE_DAYS:
                per_day[idx] += 1
        counts = [per_day[i] for i in range(BASELINE_DAYS)]
        mu = mean(counts)
        sd = pstdev(counts)
        if sd == 0:
            status = "zero_variance"
        else:
            z = (today_count - mu) / sd
            status = "ok"

    next_version = (
        db.query(func.coalesce(func.max(BaselineVersion.version), 0))
        .filter(
            BaselineVersion.employee_id == ev.employee_id,
            BaselineVersion.baseline_date == baseline_date,
        )
        .scalar()
        + 1
    )
    bv = BaselineVersion(
        employee_id=ev.employee_id,
        baseline_date=baseline_date,
        version=next_version,
        day_count=len(counts),
        mean=mu,
        std=sd,
        status=status,
        observed_count=today_count,
        z_score=z,
    )
    db.add(bv)
    db.flush()

    created = 0
    if status == "ok" and z is not None and z > Z_THRESHOLD:
        if _create_alert(
            db,
            ev.employee_id,
            RULE_BASELINE,
            day_start,
            day_end,
            {
                "baseline_version_id": bv.id,
                "baseline_version": next_version,
                "baseline_date": baseline_date.isoformat(),
                "mean": mu,
                "std": sd,
                "observed_count": today_count,
                "z_score": z,
                "z_threshold": Z_THRESHOLD,
            },
        ):
            created = 1
    return status, created


def run_detection(db: Session, events: list[Event]) -> int:
    """Evaluate all rules for newly inserted events. Returns alerts created."""
    created = 0
    by_employee: dict[int, list[Event]] = {}
    for ev in events:
        by_employee.setdefault(ev.employee_id, []).append(ev)

    for employee_id, evs in by_employee.items():
        # Serialize detection per employee so concurrent batches cannot
        # double-create the same alert.
        db.execute(
            select(Employee.id).where(Employee.id == employee_id).with_for_update()
        ).one()
        employee = db.get(Employee, employee_id)
        tz = ZoneInfo(employee.organization.timezone)

        baseline_dates_evaluated: set = set()
        for ev in sorted(evs, key=lambda e: e.occurred_at):
            if ev.event_type == "access":
                created += detect_off_hours(db, ev, tz)
            elif ev.event_type == "file_download":
                created += detect_download_burst(db, ev)
            elif ev.event_type == "usb_connect":
                created += detect_usb_first_use(db, ev)
            elif ev.event_type == "file_access":
                day = ev.occurred_at.astimezone(tz).date()
                if day not in baseline_dates_evaluated:
                    baseline_dates_evaluated.add(day)
                    _, n = evaluate_baseline(db, ev, tz)
                    created += n
    return created
