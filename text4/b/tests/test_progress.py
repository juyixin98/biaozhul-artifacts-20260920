"""Step gating, idempotent submissions, corrections and certificate rules."""
from __future__ import annotations

from tests.conftest import auth_headers


def _complete_all_steps(client, program, headers_learner, enrollment, headers_sup,
                        *, status: str = "passed") -> None:
    for step in program["steps"]:
        resp = client.post(
            f"/api/enrollments/{enrollment['id']}/steps/{step['id']}/submit",
            json={"content": f"done {step['position']}"},
            headers=headers_learner,
        )
        assert resp.status_code == 201, resp.text
        resp = client.put(
            f"/api/enrollments/{enrollment['id']}/steps/{step['id']}/evaluation",
            json={"status": status, "reason": "initial grading"},
            headers=headers_sup,
        )
        assert resp.status_code == 200, resp.text


def test_submission_requires_confirmed_enrolment(client, make_program, make_user):
    program = make_program(capacity=2)
    learner = make_user("l1", "learner")
    lh = auth_headers(learner["token"])
    resp = client.post(f"/api/programs/{program['id']}/enroll", headers=lh)
    enrollment = resp.json()  # pending, not confirmed
    step = program["steps"][0]
    resp = client.post(
        f"/api/enrollments/{enrollment['id']}/steps/{step['id']}/submit",
        json={"content": "hi"}, headers=lh,
    )
    assert resp.status_code == 409


def test_learners_can_only_submit_their_own_results(client, make_program, make_user):
    program = make_program(capacity=3)
    victim = make_user("victim", "learner")
    intruder = make_user("intruder", "learner")
    vh = auth_headers(victim["token"])

    resp = client.post(f"/api/programs/{program['id']}/enroll", headers=vh)
    victim_enr = resp.json()
    resp = client.post(
        f"/api/enrollments/{victim_enr['id']}/confirm", headers=vh
    )
    assert resp.status_code == 200

    resp = client.post(
        f"/api/enrollments/{victim_enr['id']}/steps/{program['steps'][0]['id']}/submit",
        json={"content": "hijack"},
        headers=auth_headers(intruder["token"]),
    )
    assert resp.status_code == 403

    # No result row was created for the victim.
    resp = client.get(
        f"/api/enrollments/{victim_enr['id']}/results", headers=vh
    )
    assert resp.json() == []


def test_prerequisite_gating(client, make_program, enrolled_confirmed, supervisor):
    program = make_program(capacity=3, steps=3)  # chain: 1 -> 2 -> 3
    learner = enrolled_confirmed(program, "gated")
    lh = learner["headers"]
    enrollment = learner["enrollment"]
    step1, step2, step3 = program["steps"]

    # step 2 blocked before step 1
    resp = client.post(
        f"/api/enrollments/{enrollment['id']}/steps/{step2['id']}/submit",
        json={"content": "skip"}, headers=lh,
    )
    assert resp.status_code == 409

    # submit + pass step 1
    client.post(
        f"/api/enrollments/{enrollment['id']}/steps/{step1['id']}/submit",
        json={"content": "s1"}, headers=lh,
    )
    resp = client.put(
        f"/api/enrollments/{enrollment['id']}/steps/{step1['id']}/evaluation",
        json={"status": "passed", "reason": "ok"},
        headers=auth_headers(supervisor["token"]),
    )
    assert resp.status_code == 200

    # failed step 1 blocks step 2 again
    client.patch(
        f"/api/enrollments/{enrollment['id']}/steps/{step1['id']}/correct",
        json={"status": "failed", "reason": "re-check: actually inadequate"},
        headers=auth_headers(supervisor["token"]),
    )
    resp = client.post(
        f"/api/enrollments/{enrollment['id']}/steps/{step2['id']}/submit",
        json={"content": "s2"}, headers=lh,
    )
    assert resp.status_code == 409

    # back to passed -> step 2 opens
    client.patch(
        f"/api/enrollments/{enrollment['id']}/steps/{step1['id']}/correct",
        json={"status": "passed", "reason": "re-checked evidence, passing"},
        headers=auth_headers(supervisor["token"]),
    )
    resp = client.post(
        f"/api/enrollments/{enrollment['id']}/steps/{step2['id']}/submit",
        json={"content": "s2"}, headers=lh,
    )
    assert resp.status_code == 201, resp.text


def test_repeat_submission_is_idempotent(client, make_program, enrolled_confirmed):
    program = make_program(capacity=2)
    learner = enrolled_confirmed(program, "rep")
    lh = learner["headers"]
    step = program["steps"][0]
    url = f"/api/enrollments/{learner['enrollment']['id']}/steps/{step['id']}/submit"

    r1 = client.post(url, json={"content": "first"}, headers=lh)
    r2 = client.post(url, json={"content": "second, should be ignored"}, headers=lh)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["content"] == "first"

    results = client.get(
        f"/api/enrollments/{learner['enrollment']['id']}/results", headers=lh
    ).json()
    assert len(results) == 1


def test_certificate_issued_once_and_retry_safe(client, make_program, enrolled_confirmed,
                                                supervisor):
    program = make_program(capacity=2, steps=3)
    learner = enrolled_confirmed(program, "cert")
    lh = learner["headers"]
    sh = auth_headers(supervisor["token"])
    enrollment = learner["enrollment"]

    # No certificate before completion.
    resp = client.get(
        f"/api/enrollments/{enrollment['id']}/certificate", headers=lh
    )
    assert resp.status_code == 404

    _complete_all_steps(client, program, lh, enrollment, sh)

    c1 = client.get(f"/api/enrollments/{enrollment['id']}/certificate", headers=lh)
    assert c1.status_code == 200, c1.text
    cert1 = c1.json()
    assert cert1["revoked"] is False
    assert cert1["version_id"] == program["version_id"]
    assert cert1["serial_number"]
    assert len(cert1["content_digest"]) == 64

    # Retry/re-fetch: still the same single certificate.
    c2 = client.get(f"/api/enrollments/{enrollment['id']}/certificate", headers=lh)
    cert2 = c2.json()
    assert cert2["id"] == cert1["id"]
    assert cert2["serial_number"] == cert1["serial_number"]
    assert cert2["content_digest"] == cert1["content_digest"]


def test_correction_requires_reason(client, make_program, enrolled_confirmed, supervisor):
    program = make_program(capacity=2)
    learner = enrolled_confirmed(program, "corr")
    enrollment = learner["enrollment"]
    step = program["steps"][0]
    client.post(
        f"/api/enrollments/{enrollment['id']}/steps/{step['id']}/submit",
        json={"content": "done"}, headers=learner["headers"],
    )
    sh = auth_headers(supervisor["token"])
    client.put(
        f"/api/enrollments/{enrollment['id']}/steps/{step['id']}/evaluation",
        json={"status": "passed", "reason": "initial"}, headers=sh,
    )
    resp = client.patch(
        f"/api/enrollments/{enrollment['id']}/steps/{step['id']}/correct",
        json={"status": "failed", "reason": "   "}, headers=sh,
    )
    assert resp.status_code == 409


def test_correction_to_failed_revokes_certificate(client, make_program, enrolled_confirmed,
                                                  supervisor):
    program = make_program(capacity=2, steps=2)
    learner = enrolled_confirmed(program, "revoke")
    sh = auth_headers(supervisor["token"])
    enrollment = learner["enrollment"]
    _complete_all_steps(client, program, learner["headers"], enrollment, sh)

    cert = client.get(
        f"/api/enrollments/{enrollment['id']}/certificate", headers=learner["headers"]
    ).json()
    assert cert["revoked"] is False

    # Supervisor overturns step 1: completion broken, certificate invalidated.
    resp = client.patch(
        f"/api/enrollments/{enrollment['id']}/steps/{program['steps'][0]['id']}/correct",
        json={"status": "failed", "reason": "plagiarism found on review"},
        headers=sh,
    )
    assert resp.status_code == 200, resp.text

    cert = client.get(
        f"/api/enrollments/{enrollment['id']}/certificate", headers=learner["headers"]
    ).json()
    assert cert["revoked"] is True
    assert cert["revoked_at"] is not None
    # same single certificate row / serial, now revoked
    assert cert["serial_number"] == f"SP-{program['version_id']}-{enrollment['id']:08d}"


def test_correction_back_to_passed_reissues_single_valid_certificate(
    client, make_program, enrolled_confirmed, supervisor
):
    program = make_program(capacity=2, steps=2)
    learner = enrolled_confirmed(program, "restore")
    sh = auth_headers(supervisor["token"])
    enrollment = learner["enrollment"]
    _complete_all_steps(client, program, learner["headers"], enrollment, sh)

    before = client.get(
        f"/api/enrollments/{enrollment['id']}/certificate", headers=learner["headers"]
    ).json()

    client.patch(
        f"/api/enrollments/{enrollment['id']}/steps/{program['steps'][1]['id']}/correct",
        json={"status": "failed", "reason": "grading error under appeal"},
        headers=sh,
    )
    revoked = client.get(
        f"/api/enrollments/{enrollment['id']}/certificate", headers=learner["headers"]
    ).json()
    assert revoked["revoked"] is True

    client.patch(
        f"/api/enrollments/{enrollment['id']}/steps/{program['steps'][1]['id']}/correct",
        json={"status": "passed", "reason": "appeal accepted, original work valid"},
        headers=sh,
    )
    after = client.get(
        f"/api/enrollments/{enrollment['id']}/certificate", headers=learner["headers"]
    ).json()
    assert after["revoked"] is False
    assert after["id"] == before["id"]
    assert after["serial_number"] == before["serial_number"]
