from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models import Alert, Event, EventType, UserRole
from tests.conftest import auth_headers, event, make_device, make_org, make_user

NY_TZ = timezone(timedelta(hours=-5))


def _setup_employee(db, tz="UTC"):
    org = make_org(db, 1, "Eng", tz=tz)
    admin = make_user(db, 1, "root", UserRole.admin, org_id=org.id)
    emp = make_user(db, 2, "sam", UserRole.employee, org_id=org.id)
    make_device(db, "dev1", emp.id)
    db.commit()
    return admin, emp


def test_batch_happy_path(client, db):
    _setup_employee(db)
    h = auth_headers(client, "root")
    when = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    r = client.post("/events/batch", headers=h, json={
        "events": [event("e1", "dev1", "login", when),
                   event("e2", "dev1", "file_access", when + timedelta(seconds=5))]
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["received"] == 2 and body["inserted"] == 2 and body["duplicates"] == 0


def test_batch_size_limit_and_naive_timestamp(client, db):
    _setup_employee(db)
    h = auth_headers(client, "root")
    r = client.post("/events/batch", headers=h, json={"events": []})
    assert r.status_code == 422

    big = [event(f"x{i}", "dev1", "login", datetime(2026, 9, 1, tzinfo=timezone.utc))
           for i in range(2001)]
    r = client.post("/events/batch", headers=h, json={"events": big})
    assert r.status_code == 422

    # timezone-naive timestamp is invalid
    r = client.post("/events/batch", headers=h, json={
        "events": [event("e", "dev1", "login", datetime(2026, 9, 1, 12, 0))]
    })
    assert r.status_code == 422


def test_max_batch_size_accepted(client, db):
    _setup_employee(db)
    h = auth_headers(client, "root")
    when = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    big = [event(f"m{i}", "dev1", "file_access", when + timedelta(seconds=i))
           for i in range(2000)]
    r = client.post("/events/batch", headers=h, json={"events": big})
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 2000


def test_unknown_device_rolls_back_whole_batch(client, db):
    _setup_employee(db)
    h = auth_headers(client, "root")
    when = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    r = client.post("/events/batch", headers=h, json={
        "events": [event("ok1", "dev1", "login", when),
                   event("bad1", "ghost-device", "login", when)]
    })
    assert r.status_code == 400
    assert db.scalar(select(func.count()).select_from(Event)) == 0


def test_duplicate_key_within_batch_rejected(client, db):
    _setup_employee(db)
    h = auth_headers(client, "root")
    when = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    r = client.post("/events/batch", headers=h, json={
        "events": [event("dup", "dev1", "login", when, {"a": 1}),
                   event("dup", "dev1", "file_access", when, {"a": 2})]
    })
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "duplicate_keys_within_batch"
    assert db.scalar(select(func.count()).select_from(Event)) == 0


def test_identical_retry_is_idempotent(client, db):
    _setup_employee(db)
    h = auth_headers(client, "root")
    payload = {"events": [event("e1", "dev1", "login",
                                datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc))]}
    r1 = client.post("/events/batch", headers=h, json=payload)
    assert r1.status_code == 200
    assert r1.json()["inserted"] == 1
    assert r1.json()["alerts_created"] == 1

    r2 = client.post("/events/batch", headers=h, json=payload)
    assert r2.status_code == 200
    assert r2.json()["inserted"] == 0 and r2.json()["duplicates"] == 1
    assert r2.json()["alerts_created"] == 0
    assert db.scalar(select(func.count()).select_from(Event)) == 1
    assert db.scalar(select(func.count()).select_from(Alert)) == 1


def test_same_key_different_content_conflicts_and_rolls_back(client, db):
    _setup_employee(db)
    h = auth_headers(client, "root")
    when = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    r1 = client.post("/events/batch", headers=h, json={
        "events": [event("e1", "dev1", "login", when, {"ip": "10.0.0.1"})]
    })
    assert r1.status_code == 200

    r2 = client.post("/events/batch", headers=h, json={
        "events": [event("e9", "dev1", "login", when, {"ip": "10.0.0.2"}),
                   event("e1", "dev1", "login", when, {"ip": "10.0.0.99"})]
    })
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert detail["error"] == "event_content_conflict"
    # whole batch rolled back: e9 not stored
    assert db.scalar(select(func.count()).select_from(Event)) == 1


def test_out_of_order_ingestion_does_not_duplicate_alert(client, db):
    """Late event inside an existing burst region must not create a second alert."""
    _setup_employee(db)
    h = auth_headers(client, "root")
    base = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)

    # First: 51 downloads (14:00:00 .. 14:05:00) -> one burst
    first = [event(f"dl{i}", "dev1", "file_download", base + timedelta(seconds=i * 6))
             for i in range(51)]
    r1 = client.post("/events/batch", headers=h, json={"events": first})
    assert r1.status_code == 200
    assert r1.json()["alerts_created"] == 1
    first_alert_ids = set(r1.json()["alert_ids"])

    # Late out-of-order events inserted into the same region.
    late = [event(f"late{i}", "dev1", "file_download", base + timedelta(seconds=i * 6 + 3))
            for i in range(40)]
    r2 = client.post("/events/batch", headers=h, json={"events": late})
    assert r2.status_code == 200
    assert r2.json()["alerts_created"] == 0
    burst_alerts = db.scalars(select(Alert).where(Alert.rule == "download_burst")).all()
    assert len(burst_alerts) == 1

    # Full replay of the first batch: all duplicates, still one alert.
    r3 = client.post("/events/batch", headers=h, json={"events": first})
    assert r3.json()["inserted"] == 0 and r3.json()["alerts_created"] == 0


def test_concurrent_identical_batches_insert_once(db):
    """Two threads posting the same batch concurrently: no double insert/alert."""
    import concurrent.futures

    from app.database import SessionLocal
    from app.detection.engine import ingest_batch
    from app.schemas import EventIn

    _setup_employee(db)
    when = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)

    def post():
        s = SessionLocal()
        try:
            evs = [EventIn(event_id="c1", device_key="dev1", type=EventType.login,
                           occurred_at=when, payload={"k": 1})]
            res = ingest_batch(s, evs)
            s.commit()
            return res
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: post(), range(2)))

    inserted = sum(r["inserted"] for r in results)
    assert inserted == 1
    assert db.scalar(select(func.count()).select_from(Event)) == 1
    assert db.scalar(select(func.count()).select_from(Alert)) == 1
