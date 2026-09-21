"""Step results: ownership, prerequisite gating, idempotent submission."""
from helpers import (
    confirm,
    enroll,
    learner,
    make_course,
    make_program,
    publish_version,
    setup_confirmed_enrollment,
)


def _submit(client, enrollment_id, uid, step_key, passed):
    return client.post(
        f"/enrollments/{enrollment_id}/results",
        json={"step_key": step_key, "passed": passed},
        headers=learner(uid),
    )


def test_learner_cannot_submit_for_others(client):
    ctx = setup_confirmed_enrollment(client)
    r = _submit(client, ctx["enrollment"]["id"], "learner-2", "intro", True)
    assert r.status_code == 403


def test_prerequisite_gate(client):
    ctx = setup_confirmed_enrollment(client)
    eid = ctx["enrollment"]["id"]
    r = _submit(client, eid, "learner-1", "exam", True)
    assert r.status_code == 409
    assert "intro" in r.json()["detail"]
    # After passing the prerequisite, the dependent step opens up.
    assert _submit(client, eid, "learner-1", "intro", True).status_code == 201
    assert _submit(client, eid, "learner-1", "exam", True).status_code == 201


def test_failed_prerequisite_does_not_unlock(client):
    ctx = setup_confirmed_enrollment(client)
    eid = ctx["enrollment"]["id"]
    assert _submit(client, eid, "learner-1", "intro", False).status_code == 201
    assert _submit(client, eid, "learner-1", "exam", True).status_code == 409


def test_duplicate_submission_does_not_double_count(client):
    ctx = setup_confirmed_enrollment(client)
    eid = ctx["enrollment"]["id"]
    r1 = _submit(client, eid, "learner-1", "intro", False)
    assert r1.status_code == 201
    # Retry with a different verdict: the original record stands.
    r2 = _submit(client, eid, "learner-1", "intro", True)
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    assert r2.json()["passed"] is False
    # 'intro' still not passed, so 'exam' stays locked.
    assert _submit(client, eid, "learner-1", "exam", True).status_code == 409


def test_results_require_confirmed_seat(client):
    program = make_program(client)
    publish_version(client, program["id"])
    course = make_course(client, program["id"], capacity=1)
    enrollment = enroll(client, course["id"], "learner-1").json()
    assert enrollment["status"] == "pending"
    r = _submit(client, enrollment["id"], "learner-1", "intro", True)
    assert r.status_code == 409
    confirm(client, enrollment["id"], "learner-1")
    assert _submit(client, enrollment["id"], "learner-1", "intro", True).status_code == 201


def test_unknown_step_rejected(client):
    ctx = setup_confirmed_enrollment(client)
    r = _submit(client, ctx["enrollment"]["id"], "learner-1", "nope", True)
    assert r.status_code == 404
