"""Step gating, idempotent submissions, manager corrections and certificates."""
from tests.factories import (
    create_course,
    create_published_program,
    headers,
    learner,
    make_steps,
    manager,
)


def _confirmed_enrollment(client, mgr, steps=None, capacity=5):
    _, vid = create_published_program(client, mgr, steps or make_steps([], [1], [1, 2]))
    course = create_course(client, mgr, vid, capacity=capacity)
    l1 = learner(client)
    e = client.post(f"/courses/{course}/enroll", headers=headers(l1)).json()
    client.post(f"/enrollments/{e['id']}/confirm", headers=headers(l1))
    return l1, e["id"]


def _submit(client, learner_id, enrollment_id, order, content="work", claimed=True):
    return client.post(
        f"/enrollments/{enrollment_id}/steps/{order}/submissions",
        json={"content": content, "claimed_passed": claimed},
        headers=headers(learner_id),
    )


def test_cannot_submit_before_confirming(client):
    mgr = manager(client)
    _, vid = create_published_program(client, mgr)
    course = create_course(client, mgr, vid)
    l1 = learner(client)
    e = client.post(f"/courses/{course}/enroll", headers=headers(l1)).json()
    resp = _submit(client, l1, e["id"], 1)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "SEAT_NOT_CONFIRMED"


def test_prerequisite_gating_blocks_and_then_allows(client):
    mgr = manager(client)
    l1, eid = _confirmed_enrollment(client, mgr)

    blocked = _submit(client, l1, eid, 2)
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "PREREQUISITES_NOT_PASSED"

    assert _submit(client, l1, eid, 1).status_code == 201
    assert _submit(client, l1, eid, 2).status_code == 201
    # Step 3 needs both 1 and 2.
    assert _submit(client, l1, eid, 3).status_code == 201


def test_exact_duplicate_submission_is_not_counted(client):
    mgr = manager(client)
    l1, eid = _confirmed_enrollment(client, mgr)
    first = _submit(client, l1, eid, 1, content="abc").json()
    second = _submit(client, l1, eid, 1, content="abc").json()
    third = _submit(client, l1, eid, 1, content="abc").json()
    assert first["attempt_count"] == 1
    assert second["attempt_count"] == 1
    assert third["attempt_count"] == 1

    # Different content counts as a new attempt.
    changed = _submit(client, l1, eid, 1, content="abc-v2").json()
    assert changed["attempt_count"] == 2


def test_learner_cannot_submit_for_other_enrollment(client):
    mgr = manager(client)
    l1, eid = _confirmed_enrollment(client, mgr)
    l2 = learner(client, 2)
    resp = _submit(client, l2, eid, 1)
    assert resp.status_code == 403


def test_certificate_issued_once_on_completion(client):
    mgr = manager(client)
    l1, eid = _confirmed_enrollment(client, mgr)
    for order in (1, 2, 3):
        _submit(client, l1, eid, order, content=f"s{order}")

    cert1 = client.get(f"/enrollments/{eid}/certificate", headers=headers(l1)).json()
    assert cert1["status"] == "VALID"

    # Retries/re-reads never mint a new certificate.
    _submit(client, l1, eid, 1, content="again")
    progress = client.get(f"/enrollments/{eid}/progress", headers=headers(l1)).json()
    assert progress["certificate_id"] == cert1["id"]
    assert progress["certificate_serial"] == cert1["serial"]
    cert2 = client.get(f"/enrollments/{eid}/certificate", headers=headers(l1)).json()
    assert cert2["id"] == cert1["id"]
    assert cert2["content_hash"] == cert1["content_hash"]
    assert len(cert2["serial"]) == 27 and cert2["serial"].startswith("SP-")


def test_manager_correction_requires_reason(client):
    mgr = manager(client)
    l1, eid = _confirmed_enrollment(client, mgr)
    _submit(client, l1, eid, 1)
    resp = client.post(
        f"/enrollments/{eid}/steps/1/corrections",
        json={"status": "FAILED", "reason": ""},
        headers=headers(mgr),
    )
    assert resp.status_code == 422


def test_certificate_invalidated_by_correction_and_restored(client):
    mgr = manager(client)
    l1, eid = _confirmed_enrollment(client, mgr)
    for order in (1, 2, 3):
        _submit(client, l1, eid, order)
    cert = client.get(f"/enrollments/{eid}/certificate", headers=headers(l1)).json()

    # Manager downgrades step 2 with a mandatory reason.
    resp = client.post(
        f"/enrollments/{eid}/steps/2/corrections",
        json={"status": "FAILED", "reason": "plagiarized submission"},
        headers=headers(mgr),
    )
    assert resp.status_code == 200
    revoked = client.get(f"/enrollments/{eid}/certificate", headers=headers(l1)).json()
    assert revoked["id"] == cert["id"]  # same row, not a new certificate
    assert revoked["status"] == "REVOKED"
    assert revoked["revoked_at"] is not None
    assert revoked["revoke_reason"]

    # A learner self-claim cannot override a manager FAILED judgement.
    sneaky = _submit(client, l1, eid, 2, content="new work")
    assert sneaky.status_code == 201
    assert sneaky.json()["status"] == "FAILED"
    progress = client.get(f"/enrollments/{eid}/progress", headers=headers(l1)).json()
    assert progress["certificate_status"] == "REVOKED"

    # Manager re-passes: the identical serial/digest is VALID again.
    client.post(
        f"/enrollments/{eid}/steps/2/corrections",
        json={"status": "PASSED", "reason": "re-evaluated, work is genuine"},
        headers=headers(mgr),
    )
    restored = client.get(f"/enrollments/{eid}/certificate", headers=headers(l1)).json()
    assert restored["id"] == cert["id"]
    assert restored["serial"] == cert["serial"]
    assert restored["status"] == "VALID"
    assert restored["revoked_at"] is None

    # After correction the same content can be resubmitted and counted again
    # (the manager's correction cleared the idempotency fingerprint).
    before = client.get(f"/enrollments/{eid}/progress", headers=headers(l1)).json()
    attempts_before = next(r["attempt_count"] for r in before["results"] if r["step_order"] == 2)
    again = _submit(client, l1, eid, 2, content="new work")
    assert again.json()["attempt_count"] == attempts_before + 1
    # ...and an immediate identical repeat is not counted.
    repeat = _submit(client, l1, eid, 2, content="new work")
    assert repeat.json()["attempt_count"] == attempts_before + 1


def test_no_certificate_before_all_steps(client):
    mgr = manager(client)
    l1, eid = _confirmed_enrollment(client, mgr)
    _submit(client, l1, eid, 1)
    resp = client.get(f"/enrollments/{eid}/certificate", headers=headers(l1))
    assert resp.status_code == 404


def test_learner_cannot_correct(client):
    mgr = manager(client)
    l1, eid = _confirmed_enrollment(client, mgr)
    l2 = learner(client, 2)
    resp = client.post(
        f"/enrollments/{eid}/steps/1/corrections",
        json={"status": "PASSED", "reason": "x"},
        headers=headers(l2),
    )
    assert resp.status_code == 403


def test_manager_correction_cascades_to_dependent_completion(client):
    # Three steps chained. Failing step 1 after everything passed must revoke.
    mgr = manager(client)
    l1, eid = _confirmed_enrollment(client, mgr, make_steps([], [1], [2]))
    for order in (1, 2, 3):
        _submit(client, l1, eid, order)
    assert (
        client.get(f"/enrollments/{eid}/certificate", headers=headers(l1)).json()["status"]
        == "VALID"
    )
    client.post(
        f"/enrollments/{eid}/steps/1/corrections",
        json={"status": "FAILED", "reason": "found procedural error"},
        headers=headers(mgr),
    )
    revoked = client.get(f"/enrollments/{eid}/certificate", headers=headers(l1)).json()
    assert revoked["status"] == "REVOKED"


def test_cancelling_confirmed_enrollment_revokes_certificate(client):
    mgr = manager(client)
    l1, eid = _confirmed_enrollment(client, mgr)
    for order in (1, 2, 3):
        _submit(client, l1, eid, order)
    assert (
        client.get(f"/enrollments/{eid}/certificate", headers=headers(l1)).json()["status"]
        == "VALID"
    )
    client.post(f"/enrollments/{eid}/cancel", headers=headers(l1))
    revoked = client.get(f"/enrollments/{eid}/certificate", headers=headers(l1)).json()
    assert revoked["status"] == "REVOKED"
    assert revoked["revoke_reason"] == "invalidated automatically: enrollment cancelled"


def test_correction_requires_confirmed_seat(client):
    mgr = manager(client)
    _, vid = create_published_program(client, mgr)
    course = create_course(client, mgr, vid)
    l1 = learner(client)
    e = client.post(f"/courses/{course}/enroll", headers=headers(l1)).json()
    # Enrolled but not confirmed yet.
    resp = client.post(
        f"/enrollments/{e['id']}/steps/1/corrections",
        json={"status": "PASSED", "reason": "x"},
        headers=headers(mgr),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "SEAT_NOT_CONFIRMED"
