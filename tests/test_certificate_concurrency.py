"""Certificate issuance under concurrent completion attempts:
exactly one certificate with one unique serial per enrolment.
"""
import threading

from app.database import SessionLocal
from app.models import Certificate
from app.services import progress as svc

from tests.conftest import (
    auth,
    create_course,
    create_published_program,
    linear_steps,
    make_users,
)


def test_concurrent_completion_emits_single_certificate(client):
    sup, learners = make_users(client, 1)
    learner = learners[0]
    info = create_published_program(client, sup, linear_steps(2))
    course = create_course(client, sup, info["version"]["id"], capacity=1)
    r = client.post(f"/courses/{course['id']}/enroll", headers=auth(learner))
    eid = r.json()["id"]
    client.post(f"/courses/{course['id']}/confirm", headers=auth(learner))

    steps = info["version"]["steps"]

    # Submit + pass every step via API.
    for s in steps:
        client.post(
            f"/enrollments/{eid}/steps/{s['key']}/submit",
            headers=auth(learner),
            json={"content": "done"},
        )
        client.post(
            f"/enrollments/{eid}/steps/{s['key']}/review",
            headers=auth(sup),
            json={"passed": True},
        )

    # Race multiple "recompute completion" style final reviews on the last
    # step by directly re-invoking review in concurrent sessions (each first
    # flips to failed then passed, triggering completion logic repeatedly).
    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def race():
        barrier.wait()
        db = SessionLocal()
        try:
            last_key = steps[-1]["key"]
            for _ in range(5):
                svc.review_result(
                    db, enrollment_id=eid, step_key=last_key, passed=True
                )
                db.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            db.rollback()
        finally:
            db.close()

    threads = [threading.Thread(target=race) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    db = SessionLocal()
    try:
        certs = db.query(Certificate).filter(Certificate.enrollment_id == eid).all()
        assert len(certs) == 1
        assert certs[0].status == "valid"
    finally:
        db.close()

    r = client.get(f"/enrollments/{eid}/certificate", headers=auth(learner))
    assert r.status_code == 200
    assert r.json()["status"] == "valid"
