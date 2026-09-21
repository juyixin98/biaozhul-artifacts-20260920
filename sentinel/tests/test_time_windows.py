from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models import Alert, UserRole
from tests.conftest import auth_headers, event, make_device, make_org, make_user

NY = timezone(timedelta(hours=-5))  # fixed offset for boundary arithmetic


def setup(db, tz="Etc/GMT+5"):
    org = make_org(db, 1, "Eng", tz=tz)
    make_user(db, 1, "root", UserRole.admin, org_id=org.id)
    emp = make_user(db, 2, "sam", UserRole.employee, org_id=org.id)
    make_device(db, "dev1", emp.id)
    db.commit()


def _post(client, events):
    h = auth_headers(client, "root")
    r = client.post("/events/batch", headers=h, json={"events": events})
    assert r.status_code == 200, r.text
    return r.json()


def test_off_hours_boundaries(client, db):
    setup(db)
    # 05:59 local -> outside (alert); 06:00 -> inside (no alert);
    # 21:59 -> inside (no alert); 22:00 -> outside (alert)
    cases = [
        ("e0559", 5, 59, True),
        ("e0600", 6, 0, False),
        ("e2159", 21, 59, False),
        ("e2200", 22, 0, True),
    ]
    payload = []
    for eid, hh, mm, _ in cases:
        payload.append(event(eid, "dev1", "login",
                             datetime(2026, 9, 1, hh, mm, tzinfo=NY)))
    _post(client, payload)
    alerted = {a.evidence["event_id"] for a in
               db.scalars(select(Alert).where(Alert.rule == "off_hours_access")).all()}
    assert alerted == {"e0559", "e2200"}


def test_off_hours_uses_org_timezone_not_utc(client, db):
    # UTC 10:00 is 22:00 in Asia/Shanghai -> outside in the employee's tz
    setup(db, tz="Asia/Shanghai")
    _post(client, [event("sh1", "dev1", "login", datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc))])
    # 14:00 UTC = 22:00 Shanghai -> outside
    cnt = db.scalar(select(func.count()).select_from(Alert).where(Alert.rule == "off_hours_access"))
    assert cnt == 1
    _post(client, [event("sh2", "dev1", "login", datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc))])
    # 12:00 UTC = 20:00 Shanghai -> inside; still 1
    cnt = db.scalar(select(func.count()).select_from(Alert).where(Alert.rule == "off_hours_access"))
    assert cnt == 1


def test_download_window_is_half_open(client, db):
    """Exactly 10 minutes before t is excluded; t itself is included.

    50 downloads at t-10m..t-6s would be a burst only if the t-10m boundary
    counted. Build 50 events spaced so the 51st's window includes exactly 50
    predecessors (must NOT fire) vs 51 (fires).
    """
    setup(db)
    base = datetime(2026, 9, 1, 14, 0, 0, tzinfo=NY)
    # 50 events: one exactly at base (== t-10m), 49 in the open window.
    evs = [event("edge", "dev1", "file_download", base)]
    for i in range(49):
        evs.append(event(f"in{i}", "dev1", "file_download",
                         base + timedelta(minutes=10, seconds=i + 1)))
    res = _post(client, evs)
    assert res["alerts_created"] == 0

    # One more event at +10:50 -> window (base+0:50, base+10:50] contains all 50
    # previous (base is 10m50s earlier -> excluded; 49 inside) = 50 total -> no
    # fire (strictly greater than 50 required).
    res = _post(client, [event("fiftieth", "dev1", "file_download",
                               base + timedelta(minutes=10, seconds=50))])
    assert res["alerts_created"] == 0

    # 51st inside the window -> fire.
    res = _post(client, [event("fiftyfirst", "dev1", "file_download",
                               base + timedelta(minutes=10, seconds=55))])
    assert res["alerts_created"] == 1


def test_download_burst_needs_over_50(client, db):
    setup(db)
    base = datetime(2026, 9, 1, 14, tzinfo=NY)
    evs = [event(f"d{i}", "dev1", "file_download", base + timedelta(seconds=i))
           for i in range(51)]
    res = _post(client, evs)
    burst = db.scalars(select(Alert).where(Alert.rule == "download_burst")).all()
    assert len(burst) == 1
    assert burst[0].evidence["download_count"] == 51
    # trigger is the 51st event (i=50), not earlier ones
    assert burst[0].evidence["trigger_event_id"] == "d50"


def test_usb_first_only_once_per_user(client, db):
    setup(db)
    base = datetime(2026, 9, 1, 10, tzinfo=NY)
    _post(client, [event("usb2", "dev1", "usb_connect", base + timedelta(days=2))])
    _post(client, [event("usb1", "dev1", "usb_connect", base)])  # late earlier event
    _post(client, [event("usb3", "dev1", "usb_connect", base + timedelta(days=3))])
    alerts = db.scalars(select(Alert).where(Alert.rule == "usb_first")).all()
    assert len(alerts) == 1
    # evidence always points at the earliest event, regardless of ingest order
    assert alerts[0].evidence["first_event_id"] == "usb1"


def test_late_downloads_dont_duplicate_but_can_fire_new_burst(client, db):
    """A late event that legitimately starts a NEW burst fires; old burst stays one."""
    setup(db)
    b1 = datetime(2026, 9, 1, 14, tzinfo=NY)
    first = [event(f"a{i}", "dev1", "file_download", b1 + timedelta(seconds=i * 6))
             for i in range(51)]
    _post(client, first)
    assert db.scalar(select(func.count()).select_from(Alert).where(Alert.rule == "download_burst")) == 1

    # A separate burst an hour later, fed in reverse order.
    b2 = b1 + timedelta(hours=1)
    late = [event(f"b{i}", "dev1", "file_download", b2 + timedelta(seconds=i * 6))
            for i in range(51)]
    _post(client, list(reversed(late)))
    alerts = db.scalars(select(Alert).where(Alert.rule == "download_burst")).all()
    assert len(alerts) == 2
