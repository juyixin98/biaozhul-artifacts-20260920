"""Capacity races, idempotent sign-up and deadline enforcement."""
import threading
from datetime import datetime, timedelta, timezone

from tests.factories import (
    create_course,
    create_published_program,
    headers,
    learner,
    manager,
)


def _enroll_ignoring(client, user_id, course_id, barrier, results, index):
    barrier.wait()
    resp = client.post(f"/courses/{course_id}/enroll", headers=headers(user_id))
    results[index] = resp


def test_last_seat_race_never_oversubscribes(client):
    mgr = manager(client)
    _, vid = create_published_program(client, mgr)
    capacity = 1
    course = create_course(client, mgr, vid, capacity=capacity)

    n = 8
    learner_ids = [learner(client, i) for i in range(1, n + 1)]
    results: list = [None] * n
    barrier = threading.Barrier(n)
    # Each thread gets its own TestClient/portal/DB connections.
    from fastapi.testclient import TestClient
    from app.main import app

    clients = [TestClient(app) for _ in range(n)]
    threads = [
        threading.Thread(
            target=_enroll_ignoring, args=(clients[i], learner_ids[i], course, barrier, results, i)
        )
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = []
    for r in results:
        assert r.status_code == 201, r.text
        statuses.append(r.json()["status"])

    assert statuses.count("ENROLLED") == capacity
    assert statuses.count("WAITLISTED") == n - capacity

    course_body = client.get(f"/courses/{course}").json()
    assert course_body["seats_taken"] == capacity
    assert course_body["waiting_count"] == n - capacity
    for c in clients:
        c.close()


def test_repeat_enrollment_is_idempotent(client):
    mgr = manager(client)
    _, vid = create_published_program(client, mgr)
    course = create_course(client, mgr, vid, capacity=3)
    l1 = learner(client)

    first = client.post(f"/courses/{course}/enroll", headers=headers(l1)).json()
    second = client.post(f"/courses/{course}/enroll", headers=headers(l1)).json()
    third = client.post(f"/courses/{course}/enroll", headers=headers(l1)).json()

    assert first["id"] == second["id"] == third["id"]
    assert first["seat_number"] == second["seat_number"]
    assert client.get(f"/courses/{course}").json()["seats_taken"] == 1


def test_waitlist_positions_are_sequential(client):
    mgr = manager(client)
    _, vid = create_published_program(client, mgr)
    course = create_course(client, mgr, vid, capacity=2)
    enrollments = []
    for i in range(1, 5):
        l = learner(client, i)
        e = client.post(f"/courses/{course}/enroll", headers=headers(l)).json()
        enrollments.append(e)

    assert [e["status"] for e in enrollments[:2]] == ["ENROLLED", "ENROLLED"]
    assert [e["status"] for e in enrollments[2:]] == ["WAITLISTED", "WAITLISTED"]
    assert [e["waitlist_position"] for e in enrollments[2:]] == [1, 2]


def test_enrollment_after_deadline_rejected(client):
    mgr = manager(client)
    _, vid = create_published_program(client, mgr)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    course = create_course(client, mgr, vid, capacity=3, deadline=past)
    l1 = learner(client)
    resp = client.post(f"/courses/{course}/enroll", headers=headers(l1))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "ENROLLMENT_CLOSED"


def test_repeat_enrollment_after_deadline_still_idempotent(client):
    from tests.factories import reset_clock, set_clock

    mgr = manager(client)
    t0 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    set_clock(client, mgr, t0)
    _, vid = create_published_program(client, mgr)
    course = create_course(client, mgr, vid, capacity=3, deadline=t0 + timedelta(days=1))
    l1 = learner(client)
    first = client.post(f"/courses/{course}/enroll", headers=headers(l1)).json()
    client.post(f"/enrollments/{first['id']}/confirm", headers=headers(l1))

    set_clock(client, mgr, t0 + timedelta(days=2))
    repeat = client.post(f"/courses/{course}/enroll", headers=headers(l1))
    assert repeat.status_code == 201
    assert repeat.json()["id"] == first["id"]
    reset_clock(client, mgr)
