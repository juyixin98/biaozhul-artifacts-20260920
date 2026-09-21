"""Program versioning: dependency validation, immutability, version isolation."""
from helpers import (
    SUPERVISOR,
    enroll,
    learner,
    make_course,
    make_program,
    publish_version,
    two_step_program,
)


def test_publish_rejects_cycle(client):
    steps = [
        {"step_key": "a", "title": "A", "prerequisites": ["b"]},
        {"step_key": "b", "title": "B", "prerequisites": ["a"]},
    ]
    r = client.post(
        "/programs",
        json={"title": "cyclic", "description": "", "steps": steps, "change_note": ""},
        headers=SUPERVISOR,
    )
    assert r.status_code == 422
    assert "cycle" in r.json()["detail"]


def test_publish_rejects_unknown_prerequisite(client):
    steps = [{"step_key": "a", "title": "A", "prerequisites": ["ghost"]}]
    r = client.post(
        "/programs",
        json={"title": "bad dep", "description": "", "steps": steps, "change_note": ""},
        headers=SUPERVISOR,
    )
    assert r.status_code == 422
    assert "unknown step" in r.json()["detail"]


def test_publish_rejects_duplicate_keys_and_too_many_steps(client):
    dup = [{"step_key": "a", "title": "A"}, {"step_key": "a", "title": "A2"}]
    r = client.post(
        "/programs",
        json={"title": "dup", "description": "", "steps": dup, "change_note": ""},
        headers=SUPERVISOR,
    )
    assert r.status_code == 422
    assert "duplicate" in r.json()["detail"]

    many = [{"step_key": f"s{i}", "title": f"S{i}"} for i in range(51)]
    r = client.post(
        "/programs",
        json={"title": "big", "description": "", "steps": many, "change_note": ""},
        headers=SUPERVISOR,
    )
    assert r.status_code == 422
    assert "50" in r.json()["detail"]


def test_published_version_is_immutable(client):
    program = make_program(client)
    version = publish_version(client, program["id"])
    r = client.put(
        f"/versions/{version['id']}/steps",
        json={"steps": two_step_program()},
        headers=SUPERVISOR,
    )
    assert r.status_code == 409
    # A change must go through a new version instead.
    new_version = publish_version(client, program["id"], steps=two_step_program(), note="v2")
    assert new_version["version_number"] == 2
    assert new_version["id"] != version["id"]


def test_version_isolation_across_publish_and_rollback(client):
    program = make_program(client)
    v1 = publish_version(client, program["id"])
    course = make_course(client, program["id"], capacity=5)

    # Learner binds to v1 at enrollment time.
    r = enroll(client, course["id"], "learner-1")
    assert r.status_code == 201
    enrollment = r.json()
    assert enrollment["version_id"] == v1["id"]

    # Publish v2 with different steps; the in-flight learner is unaffected.
    v2_steps = [
        {"step_key": "brand-new", "title": "New", "prerequisites": []},
        {"step_key": "exam", "title": "Exam", "prerequisites": ["brand-new"]},
    ]
    v2 = publish_version(client, program["id"], steps=v2_steps, note="v2")
    assert v2["version_number"] == 2

    r = client.get(f"/enrollments/{enrollment['id']}/progress", headers=learner("learner-1"))
    assert r.status_code == 200
    assert r.json()["version_id"] == v1["id"]
    assert [s["step_key"] for s in r.json()["steps"]] == ["intro", "exam"]

    # New enrollments now bind to v2.
    r = enroll(client, course["id"], "learner-2")
    assert r.json()["version_id"] == v2["id"]

    # Roll back the program to v1: only future enrollments are affected.
    r = client.post(
        f"/programs/{program['id']}/rollback",
        json={"version_number": 1},
        headers=SUPERVISOR,
    )
    assert r.status_code == 200
    r = enroll(client, course["id"], "learner-3")
    assert r.json()["version_id"] == v1["id"]
    # The learner on v2 keeps v2.
    r = client.get(f"/enrollments/{enrollment['id']}/progress", headers=learner("learner-1"))
    assert r.json()["version_id"] == v1["id"]


def test_rollback_requires_published_version(client):
    program = make_program(client)  # v1 stays draft
    r = client.post(
        f"/programs/{program['id']}/rollback",
        json={"version_number": 1},
        headers=SUPERVISOR,
    )
    assert r.status_code == 409


def test_program_writes_require_supervisor(client):
    r = client.post(
        "/programs",
        json={"title": "x", "description": "", "steps": two_step_program(), "change_note": ""},
        headers=learner("learner-1"),
    )
    assert r.status_code == 403
