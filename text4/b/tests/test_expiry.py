"""48-hour seat-hold expiry and waitlist promotion under a controllable clock."""
from __future__ import annotations

from tests.conftest import auth_headers


def _enroll(client, program: dict, login: str, make_user) -> dict:
    learner = make_user(login, "learner")
    resp = client.post(
        f"/api/programs/{program['id']}/enroll",
        headers=auth_headers(learner["token"]),
    )
    assert resp.status_code == 201, resp.text
    return {"token": learner["token"], "enrollment": resp.json()}


def test_seat_expires_after_48h_and_waitlist_is_promoted(client, make_program, make_user,
                                                         supervisor, frozen_now):
    base, advance = frozen_now
    program = make_program(capacity=1)
    holder = _enroll(client, program, "holder", make_user)
    waiter = _enroll(client, program, "waiter", make_user)
    assert waiter["enrollment"]["status"] == "waitlisted"

    # Just before 48h: still holding.
    advance(hours=47, minutes=59)
    client.post(
        f"/api/admin/expire-seats?program_id={program['id']}",
        headers=auth_headers(supervisor["token"]),
    )
    rows = client.get("/api/my/enrollments", headers=auth_headers(holder["token"])).json()
    assert rows[0]["status"] == "pending"

    # At/after 48h: seat expires and the oldest waiter is promoted.
    advance(hours=48, minutes=1)
    resp = client.post(
        f"/api/admin/expire-seats?program_id={program['id']}",
        headers=auth_headers(supervisor["token"]),
    )
    assert resp.status_code == 200
    assert resp.json() == {"expired": 1, "promoted": 1}

    rows = client.get("/api/my/enrollments", headers=auth_headers(holder["token"])).json()
    assert rows[0]["status"] == "expired"
    rows = client.get("/api/my/enrollments", headers=auth_headers(waiter["token"])).json()
    assert rows[0]["status"] == "pending"
    assert rows[0]["seat_granted_at"] is not None


def test_expired_seat_cannot_be_confirmed(client, make_program, make_user, frozen_now):
    base, advance = frozen_now
    program = make_program(capacity=1)
    holder = _enroll(client, program, "holder", make_user)
    _enroll(client, program, "waiter", make_user)

    advance(hours=49)
    resp = client.post(
        f"/api/enrollments/{holder['enrollment']['id']}/confirm",
        headers=auth_headers(holder["token"]),
    )
    assert resp.status_code == 409
    rows = client.get("/api/my/enrollments", headers=auth_headers(holder["token"])).json()
    assert rows[0]["status"] == "expired"


def test_confirmed_enrolment_is_not_expired(client, make_program, make_user,
                                            supervisor, frozen_now):
    base, advance = frozen_now
    program = make_program(capacity=1)
    holder = _enroll(client, program, "holder", make_user)
    client.post(
        f"/api/enrollments/{holder['enrollment']['id']}/confirm",
        headers=auth_headers(holder["token"]),
    )
    advance(hours=72)
    resp = client.post(
        f"/api/admin/expire-seats?program_id={program['id']}",
        headers=auth_headers(supervisor["token"]),
    )
    assert resp.json() == {"expired": 0, "promoted": 0}
    rows = client.get("/api/my/enrollments", headers=auth_headers(holder["token"])).json()
    assert rows[0]["status"] == "confirmed"


def test_promoted_seat_also_expires_on_its_own_clock(client, make_program, make_user,
                                                     supervisor, frozen_now):
    base, advance = frozen_now
    program = make_program(capacity=1)
    first = _enroll(client, program, "first", make_user)
    second = _enroll(client, program, "second", make_user)
    third = _enroll(client, program, "third", make_user)
    assert second["enrollment"]["status"] == "waitlisted"
    assert third["enrollment"]["status"] == "waitlisted"

    # First holder expires at 48h -> second promoted with a fresh clock.
    advance(hours=48)
    client.post(
        f"/api/admin/expire-seats?program_id={program['id']}",
        headers=auth_headers(supervisor["token"]),
    )
    second_row = client.get(
        "/api/my/enrollments", headers=auth_headers(second["token"])
    ).json()[0]
    assert second_row["status"] == "pending"

    # 47h after promotion: second still holds.
    advance(hours=47)
    client.post(
        f"/api/admin/expire-seats?program_id={program['id']}",
        headers=auth_headers(supervisor["token"]),
    )
    second_row = client.get(
        "/api/my/enrollments", headers=auth_headers(second["token"])
    ).json()[0]
    assert second_row["status"] == "pending"
    third_row = client.get(
        "/api/my/enrollments", headers=auth_headers(third["token"])
    ).json()[0]
    assert third_row["status"] == "waitlisted"

    # Another hour (96h from start, 48h after second's promotion): second expires,
    # third promoted.
    advance(hours=1)
    resp = client.post(
        f"/api/admin/expire-seats?program_id={program['id']}",
        headers=auth_headers(supervisor["token"]),
    )
    assert resp.json() == {"expired": 1, "promoted": 1}
    second_row = client.get(
        "/api/my/enrollments", headers=auth_headers(second["token"])
    ).json()[0]
    assert second_row["status"] == "expired"
    third_row = client.get(
        "/api/my/enrollments", headers=auth_headers(third["token"])
    ).json()[0]
    assert third_row["status"] == "pending"
