"""Shared helpers for API-level tests."""
from datetime import datetime, timedelta, timezone

SUPERVISOR = {"X-User-Id": "sup-1", "X-User-Role": "supervisor"}


def learner(uid: str) -> dict:
    return {"X-User-Id": uid, "X-User-Role": "learner"}


def two_step_program():
    return [
        {"step_key": "intro", "title": "Intro", "instructions": "read",
         "pass_condition": "done", "prerequisites": []},
        {"step_key": "exam", "title": "Exam", "instructions": "test",
         "pass_condition": ">=80", "prerequisites": ["intro"]},
    ]


def make_program(client, steps=None, title="Program"):
    r = client.post(
        "/programs",
        json={
            "title": title,
            "description": "demo",
            "steps": steps if steps is not None else two_step_program(),
            "change_note": "v1",
        },
        headers=SUPERVISOR,
    )
    assert r.status_code == 201, r.text
    return r.json()


def publish_version(client, program_id, steps=None, note=""):
    if steps is not None:
        r = client.post(
            f"/programs/{program_id}/versions",
            json={"steps": steps, "change_note": note},
            headers=SUPERVISOR,
        )
        assert r.status_code == 201, r.text
        version_id = r.json()["id"]
    else:
        r = client.get(f"/programs/{program_id}/versions")
        version_id = r.json()[-1]["id"]
    r = client.post(f"/versions/{version_id}/publish", headers=SUPERVISOR)
    assert r.status_code == 200, r.text
    return r.json()


def make_course(client, program_id, capacity=1, days_to_deadline=7):
    deadline = (datetime.now(timezone.utc) + timedelta(days=days_to_deadline)).isoformat()
    r = client.post(
        "/courses",
        json={
            "program_id": program_id,
            "title": "Cohort",
            "capacity": capacity,
            "enrollment_deadline": deadline,
        },
        headers=SUPERVISOR,
    )
    assert r.status_code == 201, r.text
    return r.json()


def enroll(client, course_id, learner_id, version_id=None, headers=None):
    body = {"learner_id": learner_id}
    if version_id:
        body["version_id"] = version_id
    return client.post(
        f"/courses/{course_id}/enrollments",
        json=body,
        headers=headers or learner(learner_id),
    )


def confirm(client, enrollment_id, learner_id):
    return client.post(
        f"/enrollments/{enrollment_id}/confirm", headers=learner(learner_id)
    )


def setup_confirmed_enrollment(client, capacity=1):
    """program (published) + course + one confirmed learner. Returns dict."""
    program = make_program(client)
    version = publish_version(client, program["id"])
    course = make_course(client, program["id"], capacity=capacity)
    r = enroll(client, course["id"], "learner-1")
    assert r.status_code == 201, r.text
    enrollment = r.json()
    r = confirm(client, enrollment["id"], "learner-1")
    assert r.status_code == 200, r.text
    return {
        "program": program,
        "version": version,
        "course": course,
        "enrollment": r.json(),
    }
