"""Enrollment: idempotency, capacity race, waitlist promotion, 48h expiry."""
import threading
from datetime import timedelta

from helpers import (
    SUPERVISOR,
    confirm,
    enroll,
    learner,
    make_course,
    make_program,
    publish_version,
)

from app.db import SessionLocal
from app.models import Course, Enrollment
from app.services import enrollments as svc


def _setup_course(client, capacity=1):
    program = make_program(client)
    publish_version(client, program["id"])
    return make_course(client, program["id"], capacity=capacity)


def test_duplicate_enrollment_is_idempotent(client):
    course = _setup_course(client, capacity=2)
    r1 = enroll(client, course["id"], "learner-1")
    r2 = enroll(client, course["id"], "learner-1")
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]
    # Only one seat was consumed.
    assert client.get(f"/courses/{course['id']}").json()["seats_taken"] == 1


def test_capacity_and_waitlist_order(client):
    course = _setup_course(client, capacity=1)
    e1 = enroll(client, course["id"], "learner-1").json()
    e2 = enroll(client, course["id"], "learner-2").json()
    e3 = enroll(client, course["id"], "learner-3").json()
    assert e1["status"] == "pending"
    assert e2["status"] == "waitlisted"
    assert e3["status"] == "waitlisted"


def test_cancel_promotes_oldest_waitlisted(client):
    course = _setup_course(client, capacity=1)
    e1 = enroll(client, course["id"], "learner-1").json()
    e2 = enroll(client, course["id"], "learner-2").json()
    e3 = enroll(client, course["id"], "learner-3").json()
    confirm(client, e1["id"], "learner-1")

    r = client.post(f"/enrollments/{e1['id']}/cancel", headers=learner("learner-1"))
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    # learner-2 (oldest waitlisted) is promoted to pending with a fresh deadline.
    r = client.get(f"/enrollments/{e2['id']}", headers=learner("learner-2"))
    assert r.json()["status"] == "pending"
    assert r.json()["confirm_deadline"] is not None
    r = client.get(f"/enrollments/{e3['id']}", headers=learner("learner-3"))
    assert r.json()["status"] == "waitlisted"
    assert client.get(f"/courses/{course['id']}").json()["seats_taken"] == 1


def test_seat_expires_after_48h_and_waitlist_promoted(client, fake_clock):
    course = _setup_course(client, capacity=1)
    e1 = enroll(client, course["id"], "learner-1").json()
    e2 = enroll(client, course["id"], "learner-2").json()
    assert e1["status"] == "pending"

    fake_clock.advance(hours=47, minutes=59)
    r = client.post("/jobs/expire-seats", headers=SUPERVISOR)
    assert r.json()["expired"] == 0  # not yet due

    fake_clock.advance(minutes=2)  # now past the 48h window
    r = client.post("/jobs/expire-seats", headers=SUPERVISOR)
    assert r.json()["expired"] == 1

    r = client.get(f"/enrollments/{e1['id']}", headers=learner("learner-1"))
    assert r.json()["status"] == "expired"
    r = client.get(f"/enrollments/{e2['id']}", headers=learner("learner-2"))
    assert r.json()["status"] == "pending"
    assert r.json()["confirm_deadline"] is not None
    assert client.get(f"/courses/{course['id']}").json()["seats_taken"] == 1


def test_confirm_after_deadline_releases_seat(client, fake_clock):
    course = _setup_course(client, capacity=1)
    e1 = enroll(client, course["id"], "learner-1").json()
    e2 = enroll(client, course["id"], "learner-2").json()

    fake_clock.advance(hours=49)
    r = confirm(client, e1["id"], "learner-1")
    assert r.status_code == 409
    # The lapsed hold was expired inline and the waitlist promoted.
    r = client.get(f"/enrollments/{e1['id']}", headers=learner("learner-1"))
    assert r.json()["status"] == "expired"
    r = client.get(f"/enrollments/{e2['id']}", headers=learner("learner-2"))
    assert r.json()["status"] == "pending"


def test_confirm_is_idempotent_and_self_only(client):
    course = _setup_course(client, capacity=1)
    e1 = enroll(client, course["id"], "learner-1").json()
    assert confirm(client, e1["id"], "learner-2").status_code == 403
    assert confirm(client, e1["id"], "learner-1").status_code == 200
    r = confirm(client, e1["id"], "learner-1")
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"


def test_enrollment_deadline_enforced(client, fake_clock):
    course = _setup_course(client, capacity=1)
    fake_clock.advance(days=8)  # course deadline is +7 days
    r = enroll(client, course["id"], "learner-1")
    assert r.status_code == 409


def test_concurrent_enrollment_never_oversells(client):
    course = _setup_course(client, capacity=1)
    course_id = course["id"]
    n = 12
    barrier = threading.Barrier(n)
    errors = []

    def worker(i):
        try:
            barrier.wait(timeout=10)
            with SessionLocal() as db:
                svc.enroll(db, course_id, f"racer-{i}")
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    with SessionLocal() as db:
        course_row = db.get(Course, course_id)
        assert course_row.seats_taken == 1
        rows = db.query(Enrollment).filter_by(course_id=course_id).all()
        assert len(rows) == n
        statuses = [r.status for r in rows]
        assert statuses.count("pending") == 1
        assert statuses.count("waitlisted") == n - 1


def test_concurrent_cancel_and_expire_keep_counts_consistent(client, fake_clock):
    course = _setup_course(client, capacity=1)
    e1 = enroll(client, course["id"], "learner-1").json()
    e2 = enroll(client, course["id"], "learner-2").json()
    fake_clock.advance(hours=49)

    barrier = threading.Barrier(2)
    errors = []

    def do_cancel():
        try:
            barrier.wait(timeout=10)
            with SessionLocal() as db:
                svc.cancel(db, e1["id"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def do_expire():
        try:
            barrier.wait(timeout=10)
            with SessionLocal() as db:
                svc.expire_overdue(db)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_expire)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    with SessionLocal() as db:
        course_row = db.get(Course, course["id"])
        # Exactly one seat is held afterwards: the promoted waitlisted learner.
        assert course_row.seats_taken == 1
        first = db.get(Enrollment, e1["id"])
        second = db.get(Enrollment, e2["id"])
        assert first.status in ("cancelled", "expired")
        assert second.status == "pending"
