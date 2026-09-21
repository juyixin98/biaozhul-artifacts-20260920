"""Detection rules: off-hours window, download burst window, USB first use."""
import pytest

from app.models import Alert
from tests.conftest import ev, login, post_batch, sh

RULES = {"off_hours_access", "download_burst", "usb_first_use"}


def alerts_for(db, employee_id, rule):
    return db.query(Alert).filter(Alert.employee_id == employee_id, Alert.rule == rule).all()


@pytest.mark.parametrize(
    "hour,minute,expect_alert",
    [
        (5, 59, True),    # just before allowed window
        (6, 0, False),    # window opens at 06:00 local
        (21, 59, False),  # still inside
        (22, 0, True),    # 22:00 is outside [06:00, 22:00)
        (2, 30, True),    # middle of the night
        (12, 0, False),   # midday
    ],
)
def test_off_hours_boundaries_in_org_timezone(client, seed, db, hour, minute, expect_alert):
    headers = login(client, "admin")
    r = post_batch(client, headers, [
        ev(seed.dev_alice.id, f"acc-{hour}-{minute}", "access", sh(2026, 9, 15, hour, minute))
    ])
    assert r.status_code == 200, r.text
    found = alerts_for(db, seed.alice.id, "off_hours_access")
    assert (len(found) == 1) == expect_alert


def test_off_hours_one_alert_per_local_day(client, seed, db):
    headers = login(client, "admin")
    events = [
        ev(seed.dev_alice.id, "n1", "access", sh(2026, 9, 15, 1, 0)),
        ev(seed.dev_alice.id, "n2", "access", sh(2026, 9, 15, 3, 30)),
        ev(seed.dev_alice.id, "n3", "access", sh(2026, 9, 15, 23, 45)),
    ]
    assert post_batch(client, headers, events).status_code == 200
    assert len(alerts_for(db, seed.alice.id, "off_hours_access")) == 1


def test_download_burst_exactly_50_no_alert_51_alerts(client, seed, db):
    headers = login(client, "admin")
    t = sh(2026, 9, 15, 12, 0)
    events = [ev(seed.dev_bob.id, f"d{i}", "file_download", t) for i in range(50)]
    assert post_batch(client, headers, events).status_code == 200
    assert alerts_for(db, seed.bob.id, "download_burst") == []

    r = post_batch(client, headers, [ev(seed.dev_bob.id, "d50", "file_download", t)])
    assert r.status_code == 200
    bursts = alerts_for(db, seed.bob.id, "download_burst")
    assert len(bursts) == 1
    assert bursts[0].evidence["count"] == 51


def test_download_burst_window_excludes_exact_10min_boundary(client, seed, db):
    """Window is (t-10min, t]: an event exactly 10 minutes before does not count."""
    headers = login(client, "admin")
    t = sh(2026, 9, 15, 12, 0)
    boundary = ev(seed.dev_bob.id, "boundary", "file_download", t.replace(hour=11, minute=50))
    inside = [ev(seed.dev_bob.id, f"in{i}", "file_download", t) for i in range(50)]
    assert post_batch(client, headers, [boundary] + inside).status_code == 200
    # 50 inside the window (boundary excluded) -> threshold not exceeded.
    assert alerts_for(db, seed.bob.id, "download_burst") == []

    # One more inside the window -> 51 -> alert.
    post_batch(client, headers, [ev(seed.dev_bob.id, "extra", "file_download", t)])
    bursts = alerts_for(db, seed.bob.id, "download_burst")
    assert len(bursts) == 1
    assert bursts[0].evidence["count"] == 51


def test_late_events_recompute_affected_windows_without_duplicates(client, seed, db):
    """45 downloads at 12:00 arrive first; 10 more at 11:55 arrive late.
    The window ending 12:00 must be recomputed to 55 and alert exactly once;
    retransmitting the late batch must not create duplicates."""
    headers = login(client, "admin")
    t = sh(2026, 9, 15, 12, 0)
    first = [ev(seed.dev_bob.id, f"a{i}", "file_download", t) for i in range(45)]
    assert post_batch(client, headers, first).status_code == 200
    assert alerts_for(db, seed.bob.id, "download_burst") == []

    late = [ev(seed.dev_bob.id, f"b{i}", "file_download", t.replace(hour=11, minute=55))
            for i in range(10)]
    r = post_batch(client, headers, late)
    assert r.status_code == 200
    bursts = alerts_for(db, seed.bob.id, "download_burst")
    assert len(bursts) == 1
    assert bursts[0].evidence["count"] == 55
    assert bursts[0].window_end is not None

    # Retransmit the late batch: no new events, no new alerts.
    r2 = post_batch(client, headers, late)
    assert r2.json()["inserted"] == 0
    assert len(alerts_for(db, seed.bob.id, "download_burst")) == 1


def test_usb_first_connect_alerts_once_per_device(client, seed, db):
    headers = login(client, "admin")
    t = sh(2026, 9, 15, 9, 0)
    assert post_batch(client, headers, [
        ev(seed.dev_alice.id, "usb1", "usb_connect", t)
    ]).status_code == 200
    assert len(alerts_for(db, seed.alice.id, "usb_first_use")) == 1

    # A later USB connect on the same device: not a first use.
    post_batch(client, headers, [
        ev(seed.dev_alice.id, "usb2", "usb_connect", t.replace(hour=10))
    ])
    assert len(alerts_for(db, seed.alice.id, "usb_first_use")) == 1

    # An *earlier* connect arriving late: still only one alert for the device.
    post_batch(client, headers, [
        ev(seed.dev_alice.id, "usb0", "usb_connect", t.replace(hour=8))
    ])
    found = alerts_for(db, seed.alice.id, "usb_first_use")
    assert len(found) == 1
    assert found[0].evidence["device_id"] == seed.dev_alice.id


def test_usb_first_use_is_per_device(client, seed, db):
    headers = login(client, "admin")
    t = sh(2026, 9, 15, 9, 0)
    post_batch(client, headers, [
        ev(seed.dev_alice.id, "usbA", "usb_connect", t),
        ev(seed.dev_bob.id, "usbB", "usb_connect", t),
    ])
    assert len(alerts_for(db, seed.alice.id, "usb_first_use")) == 1
    assert len(alerts_for(db, seed.bob.id, "usb_first_use")) == 1
