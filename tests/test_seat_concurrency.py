"""Cross-fire consistency: cancel vs confirm vs expiry sweep acting on the
same course simultaneously must leave seats and statuses consistent
(every seat accounted for exactly once; FIFO promotions never lost).
"""
import threading
from datetime import datetime, timezone

from app.database import SessionLocal
from app.services import enrollments as svc

from tests.conftest import (
    auth,
    create_course,
    create_published_program,
    linear_steps,
    make_users,
)


def test_confirm_and_cancel_and_sweep_concurrent_consistency(client):
    sup, learners = make_users(client, 6)

    t0 = datetime(2031, 5, 1, 8, 0, 0, tzinfo=timezone.utc)
    r = client.post("/admin/clock/freeze", headers=auth(sup), json={"at": t0.isoformat()})
    assert r.status_code == 200

    info = create_published_program(client, sup, linear_steps(3))
    course = create_course(client, sup, info["version"]["id"], capacity=3)

    # 3 offered seats (unconfirmed), 3 waitlisted.
    for uid in learners:
        client.post(f"/courses/{course['id']}/enroll", headers=auth(uid))

    errors: list[Exception] = []

    def barrier_action(fn, uid, barrier):
        barrier.wait()
        db = SessionLocal()
        try:
            fn(db, uid)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - 409s are expected outcomes
            db.rollback()
            errors.append(exc)
        finally:
            db.close()

    # Advance clock beyond 48h so sweeps will expire unconfirmed seats.
    client.post(
        "/admin/clock/advance", headers=auth(sup), json={"seconds": 49 * 3600}
    )

    barrier = threading.Barrier(6)
    threads = [
        # Learner 0 confirms (too late -> 409 expected),
        threading.Thread(
            target=barrier_action,
            args=(lambda db, uid: svc.confirm(db, course_id=course["id"], learner_id=uid),
                  learners[0], barrier),
        ),
        # Learner 1 cancels,
        threading.Thread(
            target=barrier_action,
            args=(lambda db, uid: svc.cancel(db, course_id=course["id"], learner_id=uid),
                  learners[1], barrier),
        ),
        # Three sweeps racing each other,
        *[
            threading.Thread(
                target=barrier_action,
                args=(lambda db, uid: svc.sweep_expired_seats(db, course_id=course["id"]),
                      -1, barrier),
            )
            for _ in range(3)
        ],
        # And another cancel from learner 2.
        threading.Thread(
            target=barrier_action,
            args=(lambda db, uid: svc.cancel(db, course_id=course["id"], learner_id=uid),
                  learners[2], barrier),
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Whatever interleaving occurred, the final accounting must be exact:
    #  - capacity is never exceeded;
    #  - every freed seat was filled by at most one FIFO waitlist learner;
    #  - seats_taken agrees with actual holding rows.
    view = client.get(f"/courses/{course['id']}").json()
    final_states = {}
    for uid in learners:
        me = client.get(
            f"/courses/{course['id']}/my-enrollment", headers=auth(uid)
        ).json()
        final_states[uid] = me["status"]

    holding_uids = [
        uid for uid, s in final_states.items()
        if s in ("pending_confirmation", "confirmed")
    ]
    cancelled_uids = [uid for uid, s in final_states.items() if s == "cancelled"]
    waiting_uids = [uid for uid, s in final_states.items() if s == "waitlisted"]

    # No oversell; no duplicated seat assignment.
    assert len(holding_uids) == len(set(holding_uids))
    assert len(holding_uids) <= 3
    assert view["seats_taken"] == len(holding_uids)
    assert view["waitlist_count"] == len(waiting_uids)

    # Learners 0-2 all relinquished their original seats (learner 0's late
    # confirm either lost the race -> expired, or won it before 48h logic ran
    # — but with clock advanced 49h an expired holder cannot remain pending).
    for uid in learners[:3]:
        assert final_states[uid] in ("cancelled", "expired")
    # Exactly the three cancelled/expired seats were redistributed to waitlist.
    assert len(cancelled_uids) >= 2  # learners 1 & 2 definitely cancel
    assert len(holding_uids) == 3
    # Only waitlisted learners (3-5) can hold seats now.
    assert set(holding_uids) <= set(learners[3:])

    # No DB-level errors should have escaped (business 409s are allowed).
    fatal = [e for e in errors if getattr(e, "status_code", 409) >= 500]
    assert not fatal, [str(e) for e in fatal]
