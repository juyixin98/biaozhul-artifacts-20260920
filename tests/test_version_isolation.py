"""Enrolments are pinned to a concrete version at application time and
never move when the program is republished or rolled back."""
from tests.conftest import (
    auth,
    complete_as,
    create_course,
    create_published_program,
    linear_steps,
    make_users,
)


def test_started_learning_content_is_version_pinned(client):
    sup, learners = make_users(client, 1)
    learner = learners[0]

    # v1: 2 steps
    info = create_published_program(client, sup, linear_steps(2))
    pid = info["program_id"]
    v1 = info["version"]

    course = create_course(client, sup, v1["id"], capacity=5)

    # Learner enrols, gets pinned to v1 and confirms.
    r = client.post(f"/courses/{course['id']}/enroll", headers=auth(learner))
    assert r.status_code == 201
    enrollment_id = r.json()["id"]
    assert r.json()["version_id"] == v1["id"]
    r = client.post(f"/courses/{course['id']}/confirm", headers=auth(learner))
    assert r.status_code == 200

    # Supervisor publishes v2 with a DIFFERENT step set.
    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(sup),
        json={
            "steps": linear_steps(4),
            "publish": True,
        },
    )
    r.json()

    # The course itself still references v1 (courses bind a fixed version).
    r = client.get(f"/courses/{course['id']}")
    assert r.json()["version_id"] == v1["id"]

    # The learner's enrolment still sees exactly the two v1 steps.
    r = client.get(f"/programs/versions/{v1['id']}")
    assert len(r.json()["steps"]) == 2

    # Completing the v1 program yields a certificate pinned to v1.
    complete_as(client, sup, learner, enrollment_id, ["s1", "s2"])
    r = client.get(
        f"/enrollments/{enrollment_id}/certificate", headers=auth(learner)
    )
    assert r.status_code == 200
    cert = r.json()
    assert cert["version_id"] == v1["id"]
    assert cert["content_digest"] == v1["content_digest"]
    assert cert["status"] == "valid"


def test_new_enrolment_elsewhere_can_use_new_version(client):
    sup, learners = make_users(client, 2)
    info = create_published_program(client, sup, linear_steps(2))
    v1 = info["version"]
    pid = info["program_id"]

    old_course = create_course(client, sup, v1["id"], capacity=5)
    r = client.post(f"/courses/{old_course['id']}/enroll", headers=auth(learners[0]))
    assert r.json()["version_id"] == v1["id"]

    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(sup),
        json={"steps": linear_steps(3), "publish": True},
    )
    v2 = r.json()
    new_course = create_course(client, sup, v2["id"], capacity=5)

    r = client.post(f"/courses/{new_course['id']}/enroll", headers=auth(learners[1]))
    assert r.status_code == 201
    assert r.json()["version_id"] == v2["id"]


def test_rollback_does_not_change_started_enrollment(client):
    sup, learners = make_users(client, 1)
    learner = learners[0]

    info = create_published_program(client, sup, linear_steps(2))
    pid = info["program_id"]
    v1 = info["version"]
    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(sup),
        json={"steps": linear_steps(3), "publish": True},
    )
    v2 = r.json()

    course = create_course(client, sup, v2["id"], capacity=5)
    r = client.post(f"/courses/{course['id']}/enroll", headers=auth(learner))
    assert r.json()["version_id"] == v2["id"]

    # Roll back program to v1.
    r = client.post(
        f"/programs/{pid}/rollback",
        headers=auth(sup),
        json={"version_id": v1["id"]},
    )
    assert r.status_code == 200

    # Enrolment still pinned to v2.
    r = client.get(f"/courses/{course['id']}/my-enrollment", headers=auth(learner))
    assert r.json()["version_id"] == v2["id"]
