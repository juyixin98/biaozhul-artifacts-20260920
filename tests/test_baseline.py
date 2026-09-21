"""Baseline z-score: history requirements, zero variance, anomaly, recompute."""
from datetime import timedelta

from app.models import Alert, BaselineVersion
from tests.conftest import ev, login, post_batch, sh

DAY = timedelta(days=1)
TODAY = sh(2026, 9, 20, 12, 0)


def history_events(device_id, counts_by_offset):
    """file_access events at 12:00 local for past days; offset 15..1 days back."""
    events = []
    for offset, count in counts_by_offset.items():
        for i in range(count):
            events.append(
                ev(device_id, f"hist-{offset}-{i}", "file_access",
                   TODAY - DAY * offset, {"file": f"f-{offset}-{i}"})
            )
    return events


def baselines_for(db, employee_id, date=None):
    q = db.query(BaselineVersion).filter(BaselineVersion.employee_id == employee_id)
    if date is not None:
        q = q.filter(BaselineVersion.baseline_date == date)
    return q.order_by(BaselineVersion.version).all()


def anomaly_alerts(db, employee_id):
    return db.query(Alert).filter(
        Alert.employee_id == employee_id, Alert.rule == "baseline_anomaly"
    ).all()


def test_insufficient_history_returns_explicit_status(client, seed, db):
    headers = login(client, "admin")
    r = post_batch(client, headers, [ev(seed.dev_carol.id, "x1", "file_access", TODAY)])
    assert r.status_code == 200
    rows = baselines_for(db, seed.carol.id, TODAY.date())
    assert len(rows) == 1
    assert rows[0].status == "insufficient_history"
    assert rows[0].mean is None and rows[0].z_score is None
    assert anomaly_alerts(db, seed.carol.id) == []

    # The explicit status is also visible through the API.
    resp = client.get("/api/baselines", headers=headers,
                      params={"employee_id": seed.carol.id})
    assert resp.status_code == 200
    assert resp.json()[0]["status"] == "insufficient_history"


def test_zero_variance_returns_explicit_status(client, seed, db):
    headers = login(client, "admin")
    history = history_events(seed.dev_carol.id, {d: 3 for d in range(15, 0, -1)})
    assert post_batch(client, headers, history).status_code == 200

    post_batch(client, headers, [ev(seed.dev_carol.id, "t1", "file_access", TODAY)])
    rows = baselines_for(db, seed.carol.id, TODAY.date())
    assert len(rows) == 1
    assert rows[0].status == "zero_variance"
    assert rows[0].std == 0
    assert rows[0].day_count == 14
    assert anomaly_alerts(db, seed.carol.id) == []


def test_z_score_anomaly_alert_with_evidence_and_version(client, seed, db):
    headers = login(client, "admin")
    history = history_events(seed.dev_carol.id, {d: 2 + d % 4 for d in range(15, 0, -1)})
    assert post_batch(client, headers, history).status_code == 200

    spike = [ev(seed.dev_carol.id, f"s{i}", "file_access", TODAY) for i in range(30)]
    r = post_batch(client, headers, spike)
    assert r.status_code == 200

    found = anomaly_alerts(db, seed.carol.id)
    assert len(found) == 1
    evidence = found[0].evidence
    assert evidence["z_score"] > 3.0
    assert evidence["observed_count"] == 30
    assert evidence["std"] > 0
    row = baselines_for(db, seed.carol.id, TODAY.date())[0]
    assert row.status == "ok"
    assert evidence["baseline_version"] == row.version
    assert evidence["baseline_version_id"] == row.id


def test_z_score_exactly_at_threshold_does_not_alert(client, seed, db):
    """14-day window of alternating 2/4 counts: mean=3, pstdev=1.
    Today=6 gives z=3.0, which is not > 3 -> no alert, status ok."""
    headers = login(client, "admin")
    counts = {d: (2 if d % 2 == 1 else 4) for d in range(15, 0, -1)}
    assert post_batch(client, headers, history_events(seed.dev_carol.id, counts)).status_code == 200

    today_events = [ev(seed.dev_carol.id, f"z{i}", "file_access", TODAY) for i in range(6)]
    post_batch(client, headers, today_events)

    rows = baselines_for(db, seed.carol.id, TODAY.date())
    assert rows[0].status == "ok"
    assert rows[0].mean == 3.0
    assert rows[0].std == 1.0
    assert rows[0].z_score == 3.0
    assert anomaly_alerts(db, seed.carol.id) == []


def test_recompute_preserves_investigation_state(client, seed, db):
    """Recomputing a baseline (more events same day) adds a new baseline
    version but never overwrites the existing alert or its triage state."""
    headers = login(client, "admin")
    history = history_events(seed.dev_carol.id, {d: 2 + d % 4 for d in range(15, 0, -1)})
    post_batch(client, headers, history)
    spike = [ev(seed.dev_carol.id, f"s{i}", "file_access", TODAY) for i in range(30)]
    post_batch(client, headers, spike)

    alert = anomaly_alerts(db, seed.carol.id)[0]
    original_evidence = dict(alert.evidence)

    # Triage it.
    r = client.patch(f"/api/alerts/{alert.id}",
                     json={"status": "confirmed", "version": alert.version},
                     headers=headers)
    assert r.status_code == 200
    assert r.json()["version"] == alert.version + 1

    # More events the same day -> recompute.
    more = [ev(seed.dev_carol.id, f"m{i}", "file_access", TODAY) for i in range(5)]
    post_batch(client, headers, more)

    rows = baselines_for(db, seed.carol.id, TODAY.date())
    assert [r_.version for r_ in rows] == [1, 2]
    assert rows[1].observed_count == 35

    db.expire_all()
    alert = anomaly_alerts(db, seed.carol.id)[0]
    assert len(anomaly_alerts(db, seed.carol.id)) == 1
    assert alert.status == "confirmed"
    assert alert.version == 2
    assert alert.evidence == original_evidence
