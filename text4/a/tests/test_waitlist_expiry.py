"""48h seat expiry and FIFO waitlist promotion, driven by the virtual clock."""
from datetime import datetime, timedelta, timezone

from tests.factories import (
    advance,
    create_course,
    create_published_program,
    headers,
    learner,
    manager,
    reset_clock,
    set_clock,
)

T0 = datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc)


def _fill(client, mgr, capacity=2, learners=4):
    _, vid = create_published_program(client, mgr)
    course = create_course(client, mgr, vid, capacity=capacity)
    ents = []
    for i in range(1, learners + 1):
        l = learner(client, i)
        ents.append((l, client.post(f"/courses/{course}/enroll", headers=headers(l)).json()))
    return course, ents


def test_unconfirmed_hold_expires_after_48h_and_promotes_head(client):
    mgr = manager(client)
    set_clock(client, mgr, T0)
    course, ents = _fill(client, mgr)

    # First seat holder confirms in time; second never does.
    client.post(f"/enrollments/{ents[0][1]['id']}/confirm", headers=headers(ents[0][0]))

    advance(client, mgr, hours=47)
    sweep = client.post(f"/courses/{course}/expire-holds", headers=headers(mgr))
    assert sweep.json()["expired"] == 0

    advance(client, mgr, hours=2)  # 49h total
    sweep = client.post(f"/courses/{course}/expire-holds", headers=headers(mgr))
    assert sweep.json()["expired"] == 1

    body = client.get(f"/courses/{course}").json()
    assert body["seats_taken"] == 2
    assert body["waiting_count"] == 1

    expired = client.get(
        f"/enrollments/{ents[1][1]['id']}", headers=headers(ents[1][0])
    ).json()
    assert expired["status"] == "EXPIRED"
    assert expired["seat_number"] is None

    promoted = client.get(
        f"/enrollments/{ents[2][1]['id']}", headers=headers(ents[2][0])
    ).json()
    assert promoted["status"] == "ENROLLED"
    assert promoted["seat_number"] is not None
    assert promoted["seat_expires_at"] is not None

    # The remaining waitlister keeps FIFO position 1.
    still_waiting = client.get(
        f"/enrollments/{ents[3][1]['id']}", headers=headers(ents[3][0])
    ).json()
    assert still_waiting["status"] == "WAITLISTED"
    assert still_waiting["waitlist_position"] == 1
    reset_clock(client, mgr)


def test_promoted_holder_gets_fresh_48h_window(client):
    mgr = manager(client)
    set_clock(client, mgr, T0)
    course, ents = _fill(client, mgr, capacity=1, learners=2)

    # L1 lets the seat lapse.
    advance(client, mgr, hours=48, minutes=1)
    client.post(f"/courses/{course}/expire-holds", headers=headers(mgr))
    promoted = client.get(
        f"/enrollments/{ents[1][1]['id']}", headers=headers(ents[1][0])
    ).json()
    assert promoted["status"] == "ENROLLED"
    expected = T0 + timedelta(hours=96, minutes=1)
    # The API renders timestamps in the server's local offset; compare
    # absolute instants rather than wall-clock strings.
    assert datetime.fromisoformat(promoted["seat_expires_at"]) == expected

    # Confirming inside the new window keeps the seat.
    resp = client.post(
        f"/enrollments/{ents[1][1]['id']}/confirm", headers=headers(ents[1][0])
    )
    assert resp.json()["status"] == "CONFIRMED"
    assert resp.json()["seat_expires_at"] is None
    reset_clock(client, mgr)


def test_cancel_confirmed_seat_promotes_waitlister(client):
    mgr = manager(client)
    set_clock(client, mgr, T0)
    course, ents = _fill(client, mgr)
    client.post(f"/enrollments/{ents[0][1]['id']}/confirm", headers=headers(ents[0][0]))

    resp = client.post(
        f"/enrollments/{ents[0][1]['id']}/cancel", headers=headers(ents[0][0])
    )
    assert resp.json()["status"] == "CANCELLED"

    promoted = client.get(
        f"/enrollments/{ents[2][1]['id']}", headers=headers(ents[2][0])
    ).json()
    assert promoted["status"] == "ENROLLED"
    body = client.get(f"/courses/{course}").json()
    assert body["seats_taken"] == 2  # second holder + promoted
    reset_clock(client, mgr)


def test_cancel_waitlisted_does_not_free_seat(client):
    mgr = manager(client)
    set_clock(client, mgr, T0)
    course, ents = _fill(client, mgr)
    # Waitlister (position 1) cancels: position 2 compacts to 1.
    client.post(f"/enrollments/{ents[2][1]['id']}/cancel", headers=headers(ents[2][0]))
    remaining = client.get(
        f"/enrollments/{ents[3][1]['id']}", headers=headers(ents[3][0])
    ).json()
    assert remaining["status"] == "WAITLISTED"
    assert remaining["waitlist_position"] == 1
    assert client.get(f"/courses/{course}").json()["seats_taken"] == 2
    reset_clock(client, mgr)


def test_confirm_after_expiry_rejected_and_seat_already_promoted(client):
    mgr = manager(client)
    set_clock(client, mgr, T0)
    course, ents = _fill(client, mgr, capacity=1, learners=2)
    advance(client, mgr, hours=49)
    client.post(f"/courses/{course}/expire-holds", headers=headers(mgr))

    # Stale confirm from the former holder must not steal the promoted seat.
    resp = client.post(
        f"/enrollments/{ents[0][1]['id']}/confirm", headers=headers(ents[0][0])
    )
    assert resp.status_code == 409
    promoted = client.get(
        f"/enrollments/{ents[1][1]['id']}", headers=headers(ents[1][0])
    ).json()
    assert promoted["status"] == "ENROLLED"
    assert client.get(f"/courses/{course}").json()["seats_taken"] == 1
    reset_clock(client, mgr)


def test_confirm_is_idempotent(client):
    mgr = manager(client)
    set_clock(client, mgr, T0)
    _, vid = create_published_program(client, mgr)
    course = create_course(client, mgr, vid)
    l1 = learner(client)
    e = client.post(f"/courses/{course}/enroll", headers=headers(l1)).json()
    a = client.post(f"/enrollments/{e['id']}/confirm", headers=headers(l1)).json()
    b = client.post(f"/enrollments/{e['id']}/confirm", headers=headers(l1)).json()
    assert a["status"] == b["status"] == "CONFIRMED"
    reset_clock(client, mgr)
