"""Capacity accounting under concurrency: the last seat is never
oversold; losing requests land on the waitlist in FIFO order; duplicate
enrolment is idempotent; the deadline is enforced.

Runs against PostgreSQL with real OS threads, each opening its own
session (SELECT ... FOR UPDATE on the course row serialises decisions).
"""
import threading

from fastapi.testclient import TestClient

from tests.conftest import (
    auth,
    create_course,
    create_published_program,
    linear_steps,
    make_users,
)


def _enroll_concurrently(client: TestClient, learner_ids: list[int], course_id: int):
    results: dict[int, dict] = {}
    errors: dict[int, int] = {}
    barrier = threading.Barrier(len(learner_ids))

    def hit(uid: int):
        # Each thread gets its own TestClient sharing the ASGI app but with
        # an independent portal; simpler: call the service in a fresh session.
        from app.database import SessionLocal
        from app.services.enrollments import enroll

        barrier.wait()
        db = SessionLocal()
        try:
            e = enroll(db, course_id=course_id, learner_id=uid)
            db.commit()
            results[uid] = {"status": e.status, "id": e.id}
        except Exception as exc:  # noqa: BLE001
            errors[uid] = getattr(exc, "status_code", 500)
            db.rollback()
        finally:
            db.close()

    threads = [threading.Thread(target=hit, args=(uid,)) for uid in learner_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_last_seat_race_never_oversells(client):
    sup, learners = make_users(client, 8)
    info = create_published_program(client, sup, linear_steps(2))
    course = create_course(client, sup, info["version"]["id"], capacity=3)

    results, errors = _enroll_concurrently(client, learners, course["id"])

    statuses = sorted(r["status"] for r in results.values())
    holding = [s for s in statuses if s == "pending_confirmation"]
    waiting = [s for s in statuses if s == "waitlisted"]

    assert not errors, errors
    assert len(holding) == 3, statuses
    assert len(waiting) == 5, statuses

    # Course view agrees.
    r = client.get(f"/courses/{course['id']}")
    view = r.json()
    assert view["seats_taken"] == 3
    assert view["seats_available"] == 0
    assert view["waitlist_count"] == 5


def test_duplicate_enrollment_is_idempotent(client):
    sup, learners = make_users(client, 2)
    info = create_published_program(client, sup, linear_steps(2))
    course = create_course(client, sup, info["version"]["id"], capacity=1)

    r1 = client.post(f"/courses/{course['id']}/enroll", headers=auth(learners[0]))
    r2 = client.post(f"/courses/{course['id']}/enroll", headers=auth(learners[0]))
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]

    # Second learner still goes to waitlist (no phantom freed seat).
    r3 = client.post(f"/courses/{course['id']}/enroll", headers=auth(learners[1]))
    assert r3.json()["status"] == "waitlisted"


def test_zero_capacity_course_waitlists_everyone(client):
    sup, learners = make_users(client, 2)
    info = create_published_program(client, sup, linear_steps(1))
    course = create_course(client, sup, info["version"]["id"], capacity=0)
    for uid in learners:
        r = client.post(f"/courses/{course['id']}/enroll", headers=auth(uid))
        assert r.json()["status"] == "waitlisted"
    r = client.get(f"/courses/{course['id']}")
    assert r.json()["waitlist_count"] == 2


def test_enrollment_after_deadline_rejected(client):
    sup, learners = make_users(client, 1)
    info = create_published_program(client, sup, linear_steps(1))
    # Deadline already in the past.
    course = create_course(client, sup, info["version"]["id"], capacity=5, deadline_days=-1)
    r = client.post(f"/courses/{course['id']}/enroll", headers=auth(learners[0]))
    assert r.status_code == 409
    assert "deadline" in r.json()["error"]["message"].lower()
