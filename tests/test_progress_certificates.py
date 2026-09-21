"""Step progress rules and certificate lifecycle:

* only the owning learner submits;
* prerequisite steps must be passed first;
* repeated submissions never create extra rows / never count twice;
* supervisor corrections require a reason and synchronise completion and
  certificate validity;
* exactly one certificate per enrolment with unique serial + digest;
* retry/recompletion does not issue a second certificate.
"""
from tests.conftest import (
    auth,
    complete_as,
    create_course,
    create_published_program,
    linear_steps,
    make_users,
)


def _enroll_and_confirm(client, sup, learner, version_id, capacity=5):
    course = create_course(client, sup, version_id, capacity=capacity)
    r = client.post(f"/courses/{course['id']}/enroll", headers=auth(learner))
    eid = r.json()["id"]
    r = client.post(f"/courses/{course['id']}/confirm", headers=auth(learner))
    assert r.status_code == 200
    return course, eid


def test_cannot_submit_before_prerequisite_passed(client):
    sup, learners = make_users(client, 1)
    learner = learners[0]
    info = create_published_program(client, sup, linear_steps(3))
    _, eid = _enroll_and_confirm(client, sup, learner, info["version"]["id"])

    # s2 requires s1.
    r = client.post(
        f"/enrollments/{eid}/steps/s2/submit",
        headers=auth(learner),
        json={"content": "jumping ahead"},
    )
    assert r.status_code == 409
    assert "Prerequisite" in r.json()["error"]["message"]

    # Submitting prerequisite is fine.
    r = client.post(
        f"/enrollments/{eid}/steps/s1/submit",
        headers=auth(learner),
        json={"content": "first"},
    )
    assert r.status_code == 200

    # Still blocked: s1 submitted but not passed.
    r = client.post(
        f"/enrollments/{eid}/steps/s2/submit",
        headers=auth(learner),
        json={"content": "still jumping"},
    )
    assert r.status_code == 409

    # After supervisor passes s1, s2 is open.
    r = client.post(
        f"/enrollments/{eid}/steps/s1/review",
        headers=auth(sup),
        json={"passed": True},
    )
    assert r.status_code == 200
    r = client.post(
        f"/enrollments/{eid}/steps/s2/submit",
        headers=auth(learner),
        json={"content": "now allowed"},
    )
    assert r.status_code == 200


def test_only_owner_can_submit(client):
    sup, learners = make_users(client, 2)
    info = create_published_program(client, sup, linear_steps(2))
    _, eid = _enroll_and_confirm(client, sup, learners[0], info["version"]["id"])

    r = client.post(
        f"/enrollments/{eid}/steps/s1/submit",
        headers=auth(learners[1]),
        json={"content": "not mine"},
    )
    assert r.status_code == 403


def test_waitlisted_or_unconfirmed_cannot_submit(client):
    sup, learners = make_users(client, 1)
    info = create_published_program(client, sup, linear_steps(2))
    course = create_course(client, sup, info["version"]["id"], capacity=1)
    # Learner is offered but not confirmed.
    r = client.post(f"/courses/{course['id']}/enroll", headers=auth(learners[0]))
    eid = r.json()["id"]
    r = client.post(
        f"/enrollments/{eid}/steps/s1/submit",
        headers=auth(learners[0]),
        json={"content": "x"},
    )
    assert r.status_code == 409
    assert "confirmed" in r.json()["error"]["message"]


def test_repeated_submission_is_single_row_and_increments_attempts(client):
    sup, learners = make_users(client, 1)
    learner = learners[0]
    info = create_published_program(client, sup, linear_steps(2))
    _, eid = _enroll_and_confirm(client, sup, learner, info["version"]["id"])

    for body in ("a", "b", "c"):
        r = client.post(
            f"/enrollments/{eid}/steps/s1/submit",
            headers=auth(learner),
            json={"content": body},
        )
        assert r.status_code == 200
    assert r.json()["attempts"] == 3

    results = client.get(
        f"/enrollments/{eid}/results", headers=auth(learner)
    ).json()
    s1_rows = [x for x in results if x["step_id"] == r.json()["step_id"]]
    assert len(s1_rows) == 1  # never counts twice
    assert s1_rows[0]["submission_content"] == "c"


def test_certificate_issued_once_with_unique_serial(client):
    sup, learners = make_users(client, 1)
    learner = learners[0]
    info = create_published_program(client, sup, linear_steps(3))
    _, eid = _enroll_and_confirm(client, sup, learner, info["version"]["id"])
    complete_as(client, sup, learner, eid, ["s1", "s2", "s3"])

    cert1 = client.get(
        f"/enrollments/{eid}/certificate", headers=auth(learner)
    ).json()
    assert cert1["status"] == "valid"
    assert cert1["serial_number"].startswith("SKP-")
    assert cert1["content_digest"] == info["version"]["content_digest"]

    # Re-running completion activity does not create another certificate.
    client.post(
        f"/enrollments/{eid}/steps/s1/submit",
        headers=auth(learner),
        json={"content": "resubmit"},
    )
    client.post(
        f"/enrollments/{eid}/steps/s1/review",
        headers=auth(sup),
        json={"passed": True},
    )
    cert2 = client.get(
        f"/enrollments/{eid}/certificate", headers=auth(learner)
    ).json()
    assert cert2["id"] == cert1["id"]
    assert cert2["serial_number"] == cert1["serial_number"]


def test_correction_to_failed_invalidates_certificate_and_cascades(client):
    sup, learners = make_users(client, 1)
    learner = learners[0]
    info = create_published_program(client, sup, linear_steps(3))
    _, eid = _enroll_and_confirm(client, sup, learner, info["version"]["id"])
    complete_as(client, sup, learner, eid, ["s1", "s2", "s3"])

    cert_before = client.get(
        f"/enrollments/{eid}/certificate", headers=auth(learner)
    ).json()
    assert cert_before["status"] == "valid"

    # Reason is mandatory.
    r = client.post(
        f"/enrollments/{eid}/steps/s1/correct",
        headers=auth(sup),
        json={"new_status": "failed", "reason": ""},
    )
    assert r.status_code == 422

    # Supervisor overturns s1 with a reason.
    r = client.post(
        f"/enrollments/{eid}/steps/s1/correct",
        headers=auth(sup),
        json={
            "new_status": "failed",
            "reason": "Plagiarism found on re-audit of submission.",
        },
    )
    assert r.status_code == 200
    assert r.json()["corrected"] is True

    # Completion is lost and the SAME certificate row is now invalid.
    cert_after = client.get(
        f"/enrollments/{eid}/certificate", headers=auth(learner)
    ).json()
    assert cert_after["id"] == cert_before["id"]
    assert cert_after["serial_number"] == cert_before["serial_number"]
    assert cert_after["status"] == "invalid"

    # Downstream results s2/s3 were cascade-failed (prerequisite chain broken).
    results = {x["step_id"]: x for x in client.get(
        f"/enrollments/{eid}/results", headers=auth(learner)
    ).json()}
    version_steps = {s["key"]: s for s in info["version"]["steps"]}
    for key in ("s1", "s2", "s3"):
        sid = version_steps[key]["id"]
        assert results[sid]["status"] == "failed"


def test_restoring_completion_revalidates_same_certificate(client):
    sup, learners = make_users(client, 1)
    learner = learners[0]
    info = create_published_program(client, sup, linear_steps(2))
    _, eid = _enroll_and_confirm(client, sup, learner, info["version"]["id"])
    complete_as(client, sup, learner, eid, ["s1", "s2"])
    cert = client.get(
        f"/enrollments/{eid}/certificate", headers=auth(learner)
    ).json()

    # Break then restore: correction back to passed, then re-pass s2.
    client.post(
        f"/enrollments/{eid}/steps/s1/correct",
        headers=auth(sup),
        json={"new_status": "failed", "reason": "audit"},
    )
    client.post(
        f"/enrollments/{eid}/steps/s1/correct",
        headers=auth(sup),
        json={"new_status": "passed", "reason": "appeal upheld"},
    )
    # s2 was cascade-failed; submit + review it again.
    client.post(
        f"/enrollments/{eid}/steps/s2/submit",
        headers=auth(learner),
        json={"content": "redo"},
    )
    client.post(
        f"/enrollments/{eid}/steps/s2/review",
        headers=auth(sup),
        json={"passed": True},
    )

    final = client.get(
        f"/enrollments/{eid}/certificate", headers=auth(learner)
    ).json()
    assert final["id"] == cert["id"]
    assert final["serial_number"] == cert["serial_number"]
    assert final["status"] == "valid"


def test_correction_requires_supervisor(client):
    sup, learners = make_users(client, 1)
    learner = learners[0]
    info = create_published_program(client, sup, linear_steps(2))
    _, eid = _enroll_and_confirm(client, sup, learner, info["version"]["id"])
    client.post(
        f"/enrollments/{eid}/steps/s1/submit",
        headers=auth(learner),
        json={"content": "x"},
    )
    r = client.post(
        f"/enrollments/{eid}/steps/s1/correct",
        headers=auth(learner),
        json={"new_status": "passed", "reason": "self service"},
    )
    assert r.status_code == 403


def test_no_certificate_until_all_steps_passed(client):
    sup, learners = make_users(client, 1)
    learner = learners[0]
    info = create_published_program(client, sup, linear_steps(3))
    _, eid = _enroll_and_confirm(client, sup, learner, info["version"]["id"])
    # Pass only s1 and s2.
    for key in ("s1", "s2"):
        client.post(
            f"/enrollments/{eid}/steps/{key}/submit",
            headers=auth(learner),
            json={"content": key},
        )
        client.post(
            f"/enrollments/{eid}/steps/{key}/review",
            headers=auth(sup),
            json={"passed": True},
        )
    r = client.get(f"/enrollments/{eid}/certificate", headers=auth(learner))
    assert r.status_code == 404
