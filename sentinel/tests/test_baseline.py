from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.detection.baseline import compute_baseline
from app.detection.hashes import content_hash
from app.models import (
    Alert,
    AlertInvestigation,
    AlertStatus,
    Baseline,
    BaselineStatus,
    Event,
    EventType,
    UserRole,
)
from tests.conftest import make_device, make_org, make_user

NY = ZoneInfo("America/New_York")


def _user_with_device(db, uid=2, tz="America/New_York"):
    make_org(db, 1, "Eng", tz=tz)
    make_user(db, 1, "root", UserRole.admin, org_id=1)
    emp = make_user(db, uid, "sam", UserRole.employee, org_id=1)
    make_device(db, "dev1", emp.id)
    db.commit()
    return emp


def _file_event(db, uid, when_aware, ev_type=EventType.file_access, eid=None):
    if eid is None:
        eid = f"e-{when_aware.isoformat()}-{uid}"
    db.add(Event(
        device_id=1, user_id=uid, event_id=eid, type=ev_type,
        occurred_at=when_aware, payload={},
        content_hash=content_hash(ev_type.value, when_aware, {}),
    ))


def _fill_days(db, uid, target_day, counts, tz=NY):
    """counts: list aligned to the 14 days window_lo..target-1."""
    window_lo = target_day - timedelta(days=14)
    for i, c in enumerate(counts):
        day = window_lo + timedelta(days=i)
        for j in range(c):
            when = datetime(day.year, day.month, day.day, 12, j % 60, tzinfo=tz)
            _file_event(db, uid, when, eid=f"h-{i}-{j}")


def test_insufficient_history_status(db):
    _user_with_device(db)
    target = date(2026, 9, 19)
    # only events back to day window_lo+10 -> 4 days short
    for i in range(1, 4):
        day = target - timedelta(days=i)
        _file_event(db, 2, datetime(day.year, day.month, day.day, 10, tzinfo=NY),
                    eid=f"short-{i}")
    db.flush()
    b, alert = compute_baseline(db, 2, target)
    db.commit()
    assert b.status is BaselineStatus.insufficient_history
    assert b.days_used == 3
    assert b.mean is None and b.zscore is None
    assert alert is None


def test_zero_variance_status(db):
    _user_with_device(db)
    target = date(2026, 9, 19)
    _fill_days(db, 2, target, [7] * 14)
    db.flush()
    b, alert = compute_baseline(db, 2, target)
    db.commit()
    assert b.status is BaselineStatus.zero_variance
    assert b.mean == 7.0 and b.stddev == 0.0 and b.zscore is None
    assert alert is None


def test_ok_baseline_fires_on_positive_zscore(db):
    _user_with_device(db)
    target = date(2026, 9, 19)
    _fill_days(db, 2, target, [10, 12, 9, 11, 10, 13, 9, 10, 12, 11, 10, 9, 12, 10])
    # observed target day: large spike
    for j in range(120):
        _file_event(db, 2, datetime(2026, 9, 19, 11, j % 60, tzinfo=NY),
                    EventType.file_access, eid=f"spike-{j}")
    db.flush()
    b, alert_id = compute_baseline(db, 2, target)
    db.commit()
    assert b.status is BaselineStatus.ok
    assert b.stddev > 0 and b.zscore >= 3.0
    assert alert_id is not None
    alert = db.get(Alert, alert_id)
    assert alert.baseline_id == b.id
    assert alert.evidence["baseline_version"] == 1


def test_below_threshold_no_alert(db):
    _user_with_device(db)
    target = date(2026, 9, 19)
    _fill_days(db, 2, target, [10, 12, 9, 11, 10, 13, 9, 10, 12, 11, 10, 9, 12, 10])
    for j in range(13):  # near mean
        _file_event(db, 2, datetime(2026, 9, 19, 11, j, tzinfo=NY),
                    EventType.file_access, eid=f"obs-{j}")
    db.flush()
    b, alert_id = compute_baseline(db, 2, target)
    db.commit()
    assert b.status is BaselineStatus.ok
    assert abs(b.zscore) < 3.0
    assert alert_id is None


def test_recompute_appends_version_and_keeps_investigation(db):
    """Recompute must NOT overwrite; alert + triage record must survive."""
    _user_with_device(db)
    target = date(2026, 9, 19)
    _fill_days(db, 2, target, [10, 12, 9, 11, 10, 13, 9, 10, 12, 11, 10, 9, 12, 10])
    for j in range(120):
        _file_event(db, 2, datetime(2026, 9, 19, 11, j % 60, tzinfo=NY),
                    EventType.file_access, eid=f"spike-{j}")
    db.flush()
    b1, alert_id1 = compute_baseline(db, 2, target)
    db.commit()

    # Triage the alert, creating an investigation record.
    alert = db.get(Alert, alert_id1)
    alert.status = AlertStatus.confirmed
    alert.version = 2
    db.add(AlertInvestigation(
        alert_id=alert.id, actor_id=1, action=AlertStatus.confirmed, note="investigated",
        from_version=1, to_version=2, evidence_snapshot=alert.evidence,
    ))
    db.commit()

    # Recompute (new data arrives for the same target day).
    for j in range(5):
        _file_event(db, 2, datetime(2026, 9, 19, 15, j, tzinfo=NY),
                    EventType.file_access, eid=f"late-{j}")
    db.flush()
    b2, alert_id2 = compute_baseline(db, 2, target)
    db.commit()

    assert b2.version == 2 and b1.id != b2.id
    # original baseline row untouched
    fresh_b1 = db.get(Baseline, b1.id)
    assert fresh_b1.observed_count == 120
    assert fresh_b1.version == 1
    # same alert identity (dedup per day), still confirmed, investigation intact
    assert alert_id2 is None
    alert = db.get(Alert, alert_id1)
    assert alert.status is AlertStatus.confirmed and alert.version == 2
    inv = db.scalars(select(AlertInvestigation).where(
        AlertInvestigation.alert_id == alert.id)).all()
    assert len(inv) == 1 and inv[0].note == "investigated"


def test_complete_days_include_zero_activity_days(db):
    """Only complete days feed the baseline. Zero-activity days count as zero
    provided the employee's history covers the full 14-day window."""
    _user_with_device(db)
    target = date(2026, 9, 19)
    # Event exactly on the first window day establishes full coverage; two days empty.
    counts = [1, 0, 0, 12, 10, 10, 10, 12, 9, 11, 10, 10, 10, 10]
    _fill_days(db, 2, target, counts)
    db.flush()
    b, _ = compute_baseline(db, 2, target)
    db.commit()
    assert b.status is BaselineStatus.ok
    assert b.days_used == 14
    assert len(b.daily_counts) == 14
    first_day = min(b.daily_counts)
    assert b.daily_counts[first_day] == 1
    zero_days = [d for d, v in b.daily_counts.items() if v == 0]
    assert len(zero_days) == 2
