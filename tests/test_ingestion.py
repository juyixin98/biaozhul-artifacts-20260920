"""Ingestion: dedupe, conflicts, batch atomicity, out-of-order, concurrency."""
import threading
from datetime import timedelta

from app.models import Event
from app.schemas import EventIn
from app.services.ingestion import ingest_batch
from tests.conftest import TestingSession, ev, login, post_batch, sh


def test_batch_insert_and_idempotent_retransmit(client, seed, db):
    headers = login(client, "admin")
    events = [
        ev(seed.dev_alice.id, "e1", "access", sh(2026, 9, 15, 10)),
        ev(seed.dev_alice.id, "e2", "access", sh(2026, 9, 15, 11)),
    ]
    r1 = post_batch(client, headers, events)
    assert r1.status_code == 200, r1.text
    assert r1.json()["inserted"] == 2

    # Identical retransmit: nothing new, reported as duplicates.
    r2 = post_batch(client, headers, events)
    assert r2.status_code == 200
    assert r2.json() == {"inserted": 0, "duplicates": 2, "alerts_created": 0}
    assert db.query(Event).count() == 2


def test_same_id_different_content_is_conflict_and_rolls_back(client, seed, db):
    headers = login(client, "admin")
    r1 = post_batch(client, headers, [ev(seed.dev_alice.id, "e1", "access", sh(2026, 9, 15, 10))])
    assert r1.status_code == 200

    conflict = ev(seed.dev_alice.id, "e1", "access", sh(2026, 9, 15, 10),
                  payload={"tampered": True})
    fresh = ev(seed.dev_alice.id, "e2", "access", sh(2026, 9, 15, 11))
    r2 = post_batch(client, headers, [conflict, fresh])
    assert r2.status_code == 409
    assert r2.json()["detail"]["conflicts"] == [
        {"device_id": seed.dev_alice.id, "event_id": "e1"}
    ]
    # Whole batch rolled back: the valid sibling event was not stored either.
    assert db.query(Event).count() == 1


def test_invalid_item_rolls_back_entire_batch(client, seed, db):
    headers = login(client, "admin")
    good = ev(seed.dev_alice.id, "e1", "access", sh(2026, 9, 15, 10))
    bad_device = ev(999999, "e2", "access", sh(2026, 9, 15, 10))
    r = post_batch(client, headers, [good, bad_device])
    assert r.status_code == 422
    assert db.query(Event).count() == 0

    # Naive (timezone-less) occurred_at is rejected by schema validation.
    naive = dict(ev(seed.dev_alice.id, "e3", "access", sh(2026, 9, 15, 10)))
    naive["occurred_at"] = "2026-09-15T10:00:00"
    r2 = post_batch(client, headers, [naive])
    assert r2.status_code == 422
    assert db.query(Event).count() == 0


def test_batch_size_limit(client, seed):
    headers = login(client, "admin")
    events = [ev(seed.dev_alice.id, f"e{i}", "access", sh(2026, 9, 15, 10))
              for i in range(2001)]
    r = post_batch(client, headers, events)
    assert r.status_code == 422


def test_ingest_requires_auth(client, seed):
    r = client.post("/api/events/batch",
                    json={"events": [ev(seed.dev_alice.id, "e1", "access", sh(2026, 9, 15, 10))]})
    assert r.status_code in (401, 403)


def test_out_of_order_events_are_stored_and_detected(client, seed, db):
    """Events arriving out of chronological order are all stored; detection
    still keys off event time (a late off-hours access still alerts)."""
    headers = login(client, "admin")
    late_night = ev(seed.dev_alice.id, "late", "access", sh(2026, 9, 15, 2, 0))
    midday = ev(seed.dev_alice.id, "mid", "access", sh(2026, 9, 15, 12, 0))
    # Send the later event first, the earlier (off-hours) one afterwards.
    assert post_batch(client, headers, [midday]).status_code == 200
    r = post_batch(client, headers, [late_night])
    assert r.status_code == 200
    assert r.json()["alerts_created"] == 1
    assert db.query(Event).count() == 2


def test_concurrent_retransmit_inserts_each_event_once(client, seed, db):
    """N threads ingest the same batch simultaneously: exactly one copy of
    each event survives, no thread errors out."""
    headers = login(client, "admin")
    items = [
        EventIn(device_id=seed.dev_alice.id, event_id=f"c{i}",
                event_type="access", occurred_at=sh(2026, 9, 15, 10) + timedelta(seconds=i),
                payload={})
        for i in range(100)
    ]
    errors, results = [], []

    def worker():
        session = TestingSession()
        try:
            res = ingest_batch(session, items)
            session.commit()
            results.append(res)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert sum(r["inserted"] for r in results) == 100
    assert db.query(Event).count() == 100
