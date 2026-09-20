"""Enrolment semantics: capacity, waitlist FIFO, idempotency, promotion."""
from __future__ import annotations

from tests.conftest import auth_headers


def _enroll(client, program: dict, learner: dict) -> dict:
    return client.post(
        f"/api/programs/{program['id']}/enroll",
        headers=auth_headers(learner["token"]),
    )


def test_capacity_and_waitlist_order(client, make_program, make_user):
    program = make_program(capacity=2)
    l1 = make_user("l1", "learner")
    l2 = make_user("l2", "learner")
    l3 = make_user("l3", "learner")
    l4 = make_user("l4", "learner")

    assert _enroll(client, program, l1).json()["status"] == "pending"
    assert _enroll(client, program, l2).json()["status"] == "pending"
    r3 = _enroll(client, program, l3)
    assert r3.json()["status"] == "waitlisted"
    assert r3.json()["waitlist_position"] == 1
    r4 = _enroll(client, program, l4)
    assert r4.json()["status"] == "waitlisted"
    assert r4.json()["waitlist_position"] == 2


def test_duplicate_enrollment_is_idempotent(client, make_program, make_user):
    program = make_program(capacity=1)
    learner = make_user("l1", "learner")
    first = _enroll(client, program, learner)
    second = _enroll(client, program, learner)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["status"] == first.json()["status"]

    # Same holds once waitlisted.
    other = make_user("l2", "learner")
    wl = _enroll(client, program, other)
    assert wl.json()["status"] == "waitlisted"
    again = _enroll(client, program, other)
    assert again.json()["id"] == wl.json()["id"]
    assert again.json()["status"] == "waitlisted"
    assert again.json()["waitlist_position"] == 1


def test_enrollment_deadline_enforced(client, make_program, make_user, frozen_now):
    base, advance = frozen_now
    program = make_program(capacity=5, deadline_days=1)
    learner = make_user("late", "learner")
    advance(days=2)
    resp = _enroll(client, program, learner)
    assert resp.status_code == 409
    assert "deadline" in resp.json()["detail"]


def test_cancel_releases_seat_and_promotes_oldest_waitlisted(client, make_program, make_user):
    program = make_program(capacity=1)
    holder = make_user("holder", "learner")
    w1 = make_user("w1", "learner")
    w2 = make_user("w2", "learner")

    holder_enr = _enroll(client, program, holder).json()
    assert _enroll(client, program, w1).json()["waitlist_position"] == 1
    assert _enroll(client, program, w2).json()["waitlist_position"] == 2

    resp = client.post(
        f"/api/enrollments/{holder_enr['id']}/cancel",
        headers=auth_headers(holder["token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Oldest waitlisted (w1) is promoted into a fresh pending seat.
    enrs = client.get("/api/my/enrollments", headers=auth_headers(w1["token"])).json()
    assert enrs[0]["status"] == "pending"
    assert enrs[0]["seat_granted_at"] is not None

    enrs2 = client.get("/api/my/enrollments", headers=auth_headers(w2["token"])).json()
    assert enrs2[0]["status"] == "waitlisted"
    assert enrs2[0]["waitlist_position"] == 2


def test_confirmed_cancellation_also_promotes(client, make_program, make_user):
    program = make_program(capacity=1)
    holder = make_user("holder", "learner")
    waiter = make_user("waiter", "learner")
    holder_enr = _enroll(client, program, holder).json()
    client.post(
        f"/api/enrollments/{holder_enr['id']}/confirm",
        headers=auth_headers(holder["token"]),
    )
    waiter_enr = _enroll(client, program, waiter).json()
    assert waiter_enr["status"] == "waitlisted"

    client.post(
        f"/api/enrollments/{holder_enr['id']}/cancel",
        headers=auth_headers(holder["token"]),
    )
    enrs = client.get("/api/my/enrollments", headers=auth_headers(waiter["token"])).json()
    assert enrs[0]["status"] == "pending"


def test_cannot_confirm_or_cancel_other_learners_enrollment(client, make_program, make_user):
    program = make_program(capacity=2)
    victim = make_user("victim", "learner")
    attacker = make_user("attacker", "learner")
    enr = _enroll(client, program, victim).json()

    r = client.post(
        f"/api/enrollments/{enr['id']}/confirm",
        headers=auth_headers(attacker["token"]),
    )
    assert r.status_code == 403
    r = client.post(
        f"/api/enrollments/{enr['id']}/cancel",
        headers=auth_headers(attacker["token"]),
    )
    assert r.status_code == 403

    # Untouched
    mine = client.get("/api/my/enrollments", headers=auth_headers(victim["token"])).json()
    assert mine[0]["status"] == "pending"


def test_reenroll_after_cancel_creates_new_attempt(client, make_program, make_user):
    program = make_program(capacity=1)
    learner = make_user("l1", "learner")
    first = _enroll(client, program, learner).json()
    client.post(
        f"/api/enrollments/{first['id']}/cancel",
        headers=auth_headers(learner["token"]),
    )
    second = _enroll(client, program, learner)
    assert second.status_code == 201
    assert second.json()["id"] != first["id"]
    assert second.json()["status"] == "pending"
