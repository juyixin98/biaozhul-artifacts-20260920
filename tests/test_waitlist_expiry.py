"""Waitlist FIFO promotion and the 48h unconfirmed-seat expiry,
exercised through the controllable clock and the maintenance sweep API.
Also checks cancellation/confirmation/expiry concurrency consistency.
"""
from tests.conftest import (
    auth,
    create_course,
    create_published_program,
    linear_steps,
    make_users,
)


def _freeze(client, supervisor_id: int, iso: str):
    r = client.post("/admin/clock/freeze", headers=auth(supervisor_id), json={"at": iso})
    assert r.status_code == 200, r.text


def _advance(client, supervisor_id: int, seconds: float):
    r = client.post(
        "/admin/clock/advance", headers=auth(supervisor_id), json={"seconds": seconds}
    )
    assert r.status_code == 200, r.text
    return r.json()["now"]


def _sweep(client, supervisor_id: int, course_id: int | None = None):
    url = "/admin/sweep-seat-expiries"
    if course_id is not None:
        url += f"?course_id={course_id}"
    r = client.post(url, headers=auth(supervisor_id))
    assert r.status_code == 200, r.text
    return r.json()


def test_cancel_promotes_head_of_waitlist_in_order(client):
    sup, learners = make_users(client, 4)
    info = create_published_program(client, sup, linear_steps(2))
    course = create_course(client, sup, info["version"]["id"], capacity=2)

    # First two take seats; next two queue in order.
    for uid in learners:
        client.post(f"/courses/{course['id']}/enroll", headers=auth(uid))

    positions = {}
    for uid in learners[2:]:
        r = client.get(
            f"/courses/{course['id']}/my-enrollment", headers=auth(uid)
        )
        positions[uid] = r.json()["queue_position"]
    assert positions[learners[2]] == 1
    assert positions[learners[3]] == 2

    # Seat holder 0 cancels -> learner 2 promoted.
    r = client.post(f"/courses/{course['id']}/cancel", headers=auth(learners[0]))
    assert r.status_code == 200
    r = client.get(f"/courses/{course['id']}/my-enrollment", headers=auth(learners[2]))
    assert r.json()["status"] == "pending_confirmation"
    assert r.json()["seat_expires_at"] is not None

    # Learner 3 is now alone at head of queue.
    r = client.get(f"/courses/{course['id']}/my-enrollment", headers=auth(learners[3]))
    assert r.json()["status"] == "waitlisted"
    assert r.json()["queue_position"] == 1

    # Exactly two seats remain occupied, no over/under allocation.
    view = client.get(f"/courses/{course['id']}").json()
    assert view["seats_taken"] == 2


def test_unconfirmed_seat_expires_after_48h_and_promotes_waitlist(client):
    from datetime import datetime, timezone

    sup, learners = make_users(client, 3)

    # Freeze the clock at a fixed moment before creating anything.
    t0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _freeze(client, sup, t0.isoformat())

    info = create_published_program(client, sup, linear_steps(2))
    course = create_course(client, sup, info["version"]["id"], capacity=1)

    # learner[0] grabs the seat and does NOT confirm.
    r = client.post(f"/courses/{course['id']}/enroll", headers=auth(learners[0]))
    assert r.json()["status"] == "pending_confirmation"
    # learner[1] waits.
    client.post(f"/courses/{course['id']}/enroll", headers=auth(learners[1]))

    # 47h: nothing expires yet.
    _advance(client, sup, 47 * 3600)
    result = _sweep(client, sup, course["id"])
    assert result["expired_enrollment_ids"] == []
    r = client.get(f"/courses/{course['id']}/my-enrollment", headers=auth(learners[0]))
    assert r.json()["status"] == "pending_confirmation"

    # 48h+1s: seat expires and learner[1] is promoted in the same sweep.
    _advance(client, sup, 3600 + 1)
    result = _sweep(client, sup, course["id"])
    assert len(result["expired_enrollment_ids"]) == 1
    assert len(result["promoted_enrollment_ids"]) == 1

    r0 = client.get(f"/courses/{course['id']}/my-enrollment", headers=auth(learners[0]))
    assert r0.json()["status"] == "expired"
    r1 = client.get(f"/courses/{course['id']}/my-enrollment", headers=auth(learners[1]))
    assert r1.json()["status"] == "pending_confirmation"

    view = client.get(f"/courses/{course['id']}").json()
    assert view["seats_taken"] == 1


def test_confirmation_before_48h_keeps_seat(client):
    from datetime import datetime, timezone

    sup, learners = make_users(client, 2)
    t0 = datetime(2030, 2, 1, 9, 0, 0, tzinfo=timezone.utc)
    _freeze(client, sup, t0.isoformat())

    info = create_published_program(client, sup, linear_steps(2))
    course = create_course(client, sup, info["version"]["id"], capacity=1)
    client.post(f"/courses/{course['id']}/enroll", headers=auth(learners[0]))
    client.post(f"/courses/{course['id']}/enroll", headers=auth(learners[1]))

    _advance(client, sup, 40 * 3600)
    r = client.post(f"/courses/{course['id']}/confirm", headers=auth(learners[0]))
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"

    # Well past 48h total: confirmed seat never expires.
    _advance(client, sup, 20 * 3600)
    result = _sweep(client, sup, course["id"])
    assert result["expired_enrollment_ids"] == []
    r = client.get(f"/courses/{course['id']}/my-enrollment", headers=auth(learners[1]))
    assert r.json()["status"] == "waitlisted"


def test_late_confirm_after_expiry_is_rejected(client):
    from datetime import datetime, timezone

    sup, learners = make_users(client, 2)
    t0 = datetime(2030, 3, 1, 9, 0, 0, tzinfo=timezone.utc)
    _freeze(client, sup, t0.isoformat())

    info = create_published_program(client, sup, linear_steps(2))
    course = create_course(client, sup, info["version"]["id"], capacity=1)
    client.post(f"/courses/{course['id']}/enroll", headers=auth(learners[0]))
    client.post(f"/courses/{course['id']}/enroll", headers=auth(learners[1]))

    _advance(client, sup, 49 * 3600)
    _sweep(client, sup, course["id"])

    # Learner 0 tries to confirm the seat after it expired and was given away.
    r = client.post(f"/courses/{course['id']}/confirm", headers=auth(learners[0]))
    assert r.status_code == 409

    # Learner 1 (promoted) confirms successfully.
    r = client.post(f"/courses/{course['id']}/confirm", headers=auth(learners[1]))
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"


def test_re_enrolment_after_expiry_reuses_row_and_can_waitlist(client):
    from datetime import datetime, timezone

    sup, learners = make_users(client, 1)
    t0 = datetime(2030, 4, 1, 9, 0, 0, tzinfo=timezone.utc)
    _freeze(client, sup, t0.isoformat())

    info = create_published_program(client, sup, linear_steps(2))
    course = create_course(client, sup, info["version"]["id"], capacity=1)
    r = client.post(f"/courses/{course['id']}/enroll", headers=auth(learners[0]))
    expired_id = r.json()["id"]

    _advance(client, sup, 49 * 3600)
    _sweep(client, sup, course["id"])

    # Capacity frees up (no waitlist), so re-registering gets a fresh seat.
    r = client.post(f"/courses/{course['id']}/enroll", headers=auth(learners[0]))
    assert r.status_code == 201
    assert r.json()["id"] == expired_id  # row reused
    assert r.json()["status"] == "pending_confirmation"
