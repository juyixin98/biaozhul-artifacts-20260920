"""Certificates: single issuance, idempotent retries, correction-driven
revocation and re-validation."""
from helpers import SUPERVISOR, learner, setup_confirmed_enrollment


def _pass_all(client, enrollment_id, uid="learner-1"):
    for step in ("intro", "exam"):
        r = client.post(
            f"/enrollments/{enrollment_id}/results",
            json={"step_key": step, "passed": True},
            headers=learner(uid),
        )
        assert r.status_code in (200, 201), r.text
        if step == "exam":
            return r.json()


def _cert(client, enrollment_id, uid="learner-1"):
    return client.get(
        f"/enrollments/{enrollment_id}/certificate", headers=learner(uid)
    )


def test_certificate_issued_once_with_serial_and_digest(client):
    ctx = setup_confirmed_enrollment(client)
    eid = ctx["enrollment"]["id"]
    assert _cert(client, eid).status_code == 404  # nothing before completion

    _pass_all(client, eid)
    r = _cert(client, eid)
    assert r.status_code == 200
    cert = r.json()
    assert cert["status"] == "valid"
    assert cert["serial_number"].startswith("SP-")
    assert len(cert["content_digest"]) == 64
    assert cert["version_id"] == ctx["version"]["id"]

    # Retrying the final submission must not issue a second certificate.
    client.post(
        f"/enrollments/{eid}/results",
        json={"step_key": "exam", "passed": True},
        headers=learner("learner-1"),
    )
    again = _cert(client, eid).json()
    assert again["id"] == cert["id"]
    assert again["serial_number"] == cert["serial_number"]

    # Public verification by serial number works.
    r = client.get(f"/certificates/{cert['serial_number']}")
    assert r.status_code == 200
    assert r.json()["status"] == "valid"


def test_correction_requires_reason_and_supervisor(client):
    ctx = setup_confirmed_enrollment(client)
    eid = ctx["enrollment"]["id"]
    _pass_all(client, eid)
    result_id = client.get(
        f"/enrollments/{eid}/progress", headers=learner("learner-1")
    )
    # grab a result id via the results endpoint replay
    r = client.post(
        f"/enrollments/{eid}/results",
        json={"step_key": "exam", "passed": True},
        headers=learner("learner-1"),
    )
    result_id = r.json()["id"]

    r = client.post(
        f"/results/{result_id}/corrections",
        json={"passed": False, "reason": "  "},
        headers=SUPERVISOR,
    )
    assert r.status_code == 422
    r = client.post(
        f"/results/{result_id}/corrections",
        json={"passed": False, "reason": "audit"},
        headers=learner("learner-1"),
    )
    assert r.status_code == 403


def test_correction_revokes_and_revalidates_certificate(client):
    ctx = setup_confirmed_enrollment(client)
    eid = ctx["enrollment"]["id"]
    _pass_all(client, eid)
    cert = _cert(client, eid).json()
    assert cert["status"] == "valid"

    exam_result = client.post(
        f"/enrollments/{eid}/results",
        json={"step_key": "exam", "passed": True},
        headers=learner("learner-1"),
    ).json()

    # Supervisor flips the exam to failed: certificate is revoked.
    r = client.post(
        f"/results/{exam_result['id']}/corrections",
        json={"passed": False, "reason": "proctor reported cheating"},
        headers=SUPERVISOR,
    )
    assert r.status_code == 200
    assert r.json()["passed"] is False
    revoked = _cert(client, eid).json()
    assert revoked["status"] == "revoked"
    assert revoked["revoked_at"] is not None
    # Public verification reflects the revocation.
    assert client.get(f"/certificates/{cert['serial_number']}").json()["status"] == "revoked"

    # Supervisor corrects back to passed: the SAME certificate becomes valid
    # again — no duplicate is issued.
    r = client.post(
        f"/results/{exam_result['id']}/corrections",
        json={"passed": True, "reason": "appeal upheld"},
        headers=SUPERVISOR,
    )
    assert r.status_code == 200
    restored = _cert(client, eid).json()
    assert restored["status"] == "valid"
    assert restored["id"] == cert["id"]
    assert restored["serial_number"] == cert["serial_number"]
