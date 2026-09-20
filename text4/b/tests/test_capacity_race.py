"""True concurrency tests: multiple sessions/threads racing for seats.

These bypass the HTTP client because the interesting property lives in the
service layer's row locking.  Every thread uses its own SQLAlchemy session on
the shared test PostgreSQL database.
"""
from __future__ import annotations

import threading

from sqlalchemy import select

from app.clock import set_clock
from app.services import enrollment_service
from app.statuses import ENR_CONFIRMED, ENR_PENDING, ENR_WAITLISTED
from tests.conftest import BASE_TIME


def _learner_ids(client, make_user, count: int, prefix: str) -> list[int]:
    ids = []
    for i in range(count):
        body = make_user(f"{prefix}{i}", "learner")
        ids.append(body["user"]["id"])
    return ids


def test_concurrent_enrolment_never_exceeds_capacity(client, db_engine, session_factory,
                                                     make_program, make_user):
    capacity = 3
    program = make_program(capacity=capacity)
    learner_ids = _learner_ids(client, make_user, 12, "racer")

    results: list[int | Exception] = []
    barrier = threading.Barrier(len(learner_ids))

    def worker(learner_id: int):
        session = session_factory()
        try:
            set_clock(BASE_TIME)
            barrier.wait(timeout=10)
            enrollment = enrollment_service.enroll(session, program["id"], learner_id)
            results.append(enrollment.status)
        except Exception as exc:  # pragma: no cover - surfaced by assertions
            results.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(lid,)) for lid in learner_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == len(learner_ids)
    assert not any(isinstance(r, Exception) for r in results), results
    seats = [r for r in results if r in (ENR_PENDING, ENR_CONFIRMED)]
    waitlist = [r for r in results if r == ENR_WAITLISTED]
    assert len(seats) == capacity
    assert len(waitlist) == len(learner_ids) - capacity

    # Database invariant agrees.
    audit = session_factory()
    from app.models import Enrollment
    db_seats = audit.scalars(
        select(Enrollment).where(
            Enrollment.program_id == program["id"],
            Enrollment.status.in_((ENR_PENDING, ENR_CONFIRMED)),
        )
    ).all()
    assert len(db_seats) == capacity
    positions = [
        e.waitlist_position
        for e in audit.scalars(
            select(Enrollment).where(
                Enrollment.program_id == program["id"],
                Enrollment.status == ENR_WAITLISTED,
            )
        ).all()
    ]
    assert sorted(positions) == list(range(1, len(positions) + 1))
    audit.close()


def test_concurrent_duplicate_enrolment_is_idempotent(client, db_engine, session_factory,
                                                      make_program, make_user):
    program = make_program(capacity=5)
    learner = make_user("dupe", "learner")
    learner_id = learner["user"]["id"]

    outcomes: list = []
    barrier = threading.Barrier(2)

    def worker():
        session = session_factory()
        try:
            set_clock(BASE_TIME)
            barrier.wait(timeout=10)
            outcomes.append(
                enrollment_service.enroll(session, program["id"], learner_id).id
            )
        except Exception as exc:  # pragma: no cover
            outcomes.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(isinstance(o, int) for o in outcomes), outcomes
    assert len(set(outcomes)) == 1  # both calls returned the same enrolment

    audit = session_factory()
    from app.models import Enrollment
    count = len(
        audit.scalars(
            select(Enrollment).where(
                Enrollment.program_id == program["id"],
                Enrollment.learner_id == learner_id,
            )
        ).all()
    )
    assert count == 1
    audit.close()
