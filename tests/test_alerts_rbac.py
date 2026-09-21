"""Alert triage: optimistic locking, RBAC scoping, concurrent triage."""
import threading

import pytest

from app.models import Alert
from app.services.triage import VersionConflictError, update_alert_status
from tests.conftest import TestingSession, ev, login, post_batch, sh


@pytest.fixture
def seeded_alerts(client, seed, db):
    """One off-hours alert each for alice, bob, carol, eve."""
    headers = login(client, "admin")
    events = [
        ev(seed.dev_alice.id, "oh-a", "access", sh(2026, 9, 15, 2, 0)),
        ev(seed.dev_bob.id, "oh-b", "access", sh(2026, 9, 15, 2, 0)),
        ev(seed.dev_carol.id, "oh-c", "access", sh(2026, 9, 15, 2, 0)),
        ev(seed.dev_eve.id, "oh-e", "access", sh(2026, 9, 15, 2, 0)),
    ]
    assert post_batch(client, headers, events).status_code == 200
    return {a.employee_id: a for a in db.query(Alert).all()}


def test_alerts_require_auth(client, seeded_alerts):
    assert client.get("/api/alerts").status_code in (401, 403)
    r = client.get("/api/alerts", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


def test_admin_sees_all_alerts(client, seed, seeded_alerts):
    headers = login(client, "admin")
    r = client.get("/api/alerts", headers=headers)
    assert r.status_code == 200
    assert {a["employee_id"] for a in r.json()} == {
        seed.alice.id, seed.bob.id, seed.carol.id, seed.eve.id
    }


def test_manager_sees_only_direct_reports(client, seed, seeded_alerts):
    headers = login(client, "manager")
    r = client.get("/api/alerts", headers=headers)
    assert {a["employee_id"] for a in r.json()} == {
        seed.alice.id, seed.bob.id, seed.carol.id
    }  # eve has no manager -> invisible


def test_analyst_sees_only_authorized_departments(client, seed, seeded_alerts):
    headers = login(client, "analyst")
    r = client.get("/api/alerts", headers=headers)
    assert {a["employee_id"] for a in r.json()} == {seed.alice.id, seed.bob.id}


def test_out_of_scope_detail_and_patch_are_forbidden(client, seed, seeded_alerts):
    eve_alert = seeded_alerts[seed.eve.id]
    manager = login(client, "manager")
    assert client.get(f"/api/alerts/{eve_alert.id}", headers=manager).status_code == 403
    r = client.patch(f"/api/alerts/{eve_alert.id}",
                     json={"status": "confirmed", "version": 1}, headers=manager)
    assert r.status_code == 403

    carol_alert = seeded_alerts[seed.carol.id]  # Finance: outside analyst scope
    analyst = login(client, "analyst")
    r = client.patch(f"/api/alerts/{carol_alert.id}",
                     json={"status": "confirmed", "version": 1}, headers=analyst)
    assert r.status_code == 403


def test_patch_status_transitions_and_version_bump(client, seed, seeded_alerts):
    headers = login(client, "admin")
    alert = seeded_alerts[seed.alice.id]
    for status in ("confirmed", "investigated", "false_positive"):
        current = client.get(f"/api/alerts/{alert.id}", headers=headers).json()
        r = client.patch(f"/api/alerts/{alert.id}",
                         json={"status": status, "version": current["version"]},
                         headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == status
        assert r.json()["version"] == current["version"] + 1


def test_stale_version_conflict(client, seed, seeded_alerts):
    headers = login(client, "admin")
    alert = seeded_alerts[seed.alice.id]
    r1 = client.patch(f"/api/alerts/{alert.id}",
                      json={"status": "confirmed", "version": 1}, headers=headers)
    assert r1.status_code == 200
    r2 = client.patch(f"/api/alerts/{alert.id}",
                      json={"status": "investigated", "version": 1}, headers=headers)
    assert r2.status_code == 409
    # State is the first writer's.
    current = client.get(f"/api/alerts/{alert.id}", headers=headers).json()
    assert current["status"] == "confirmed"
    assert current["version"] == 2


def test_concurrent_triage_exactly_one_winner(client, seed, seeded_alerts):
    """Two analysts triage the same alert at the same version concurrently:
    exactly one update commits, the other gets a version conflict."""
    alert = seeded_alerts[seed.alice.id]
    outcomes = []

    def worker(status):
        session = TestingSession()
        try:
            update_alert_status(session, alert.id, status, version=1)
            session.commit()
            outcomes.append("ok")
        except VersionConflictError:
            session.rollback()
            outcomes.append("conflict")
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(s,))
               for s in ("confirmed", "investigated")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["conflict", "ok"]
    session = TestingSession()
    try:
        refreshed = session.get(Alert, alert.id)
        assert refreshed.version == 2
        assert refreshed.status in ("confirmed", "investigated")
    finally:
        session.close()
