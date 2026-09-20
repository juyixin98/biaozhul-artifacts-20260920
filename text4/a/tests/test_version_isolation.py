"""Pinned-version isolation: publish/rollback never moves in-progress learners."""
from tests.factories import (
    create_course,
    create_published_program,
    headers,
    learner,
    make_steps,
    manager,
)


def test_enrollment_is_pinned_to_its_version(client):
    mgr = manager(client)
    pid, v1 = create_published_program(client, mgr, make_steps([], [1]))
    course1 = create_course(client, mgr, v1, capacity=5)

    l1 = learner(client, 1)
    e1 = client.post(f"/courses/{course1}/enroll", headers=headers(l1)).json()
    assert e1["version_id"] == v1

    # Publish a new version and roll the program pointer forward.
    v2 = client.post(
        f"/programs/{pid}/versions",
        json={"steps": make_steps([], [1], [1, 2])},
        headers=headers(mgr),
    ).json()["id"]
    client.post(f"/versions/{v2}/publish", headers=headers(mgr))

    # The existing enrollment still points at v1 and sees v1's two steps.
    progress = client.get(f"/enrollments/{e1['id']}/progress", headers=headers(l1)).json()
    assert progress["version_id"] == v1
    assert progress["total_steps"] == 2

    # A course for v2 and a fresh learner is independent.
    course2 = create_course(client, mgr, v2, capacity=5)
    l2 = learner(client, 2)
    e2 = client.post(f"/courses/{course2}/enroll", headers=headers(l2)).json()
    assert e2["version_id"] == v2
    progress2 = client.get(f"/enrollments/{e2['id']}/progress", headers=headers(l2)).json()
    assert progress2["total_steps"] == 3


def test_rollback_pointer_does_not_change_started_learning(client):
    mgr = manager(client)
    pid, v1 = create_published_program(client, mgr, make_steps([], [1]))
    course1 = create_course(client, mgr, v1, capacity=5)
    l1 = learner(client, 1)
    e1 = client.post(f"/courses/{course1}/enroll", headers=headers(l1)).json()
    client.post(f"/enrollments/{e1['id']}/confirm", headers=headers(l1))

    v2 = client.post(
        f"/programs/{pid}/versions",
        json={"steps": make_steps([], [1], [1, 2], [1, 2, 3])},
        headers=headers(mgr),
    ).json()["id"]
    client.post(f"/versions/{v2}/publish", headers=headers(mgr))
    # Roll back new enrollments to v1.
    resp = client.put(
        f"/programs/{pid}/current-version",
        json={"version_id": v1},
        headers=headers(mgr),
    )
    assert resp.status_code == 200

    progress = client.get(f"/enrollments/{e1['id']}/progress", headers=headers(l1)).json()
    assert progress["version_id"] == v1
    assert progress["total_steps"] == 2
    # The pinned enrollment can never touch v2 step 3.
    resp = client.post(
        f"/enrollments/{e1['id']}/steps/3/submissions",
        json={"content": "x", "claimed_passed": True},
        headers=headers(l1),
    )
    assert resp.status_code == 404


def test_progress_of_other_learner_forbidden(client):
    mgr = manager(client)
    _, v1 = create_published_program(client, mgr)
    course = create_course(client, mgr, v1)
    l1 = learner(client, 1)
    l2 = learner(client, 2)
    e1 = client.post(f"/courses/{course}/enroll", headers=headers(l1)).json()
    resp = client.get(f"/enrollments/{e1['id']}/progress", headers=headers(l2))
    assert resp.status_code == 403
