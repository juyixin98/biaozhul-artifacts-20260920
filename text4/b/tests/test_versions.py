"""Version isolation, immutability and step-dependency validation."""
from __future__ import annotations

from tests.conftest import auth_headers


# ---------- immutability ----------

def test_published_version_is_immutable(client, make_program, supervisor):
    program = make_program(capacity=2)
    headers = auth_headers(supervisor["token"])
    resp = client.put(
        f"/api/programs/versions/{program['version_id']}/steps",
        json={"steps": [
            {"title": "X", "instruction": "do x", "pass_condition": "x done",
             "prerequisite_positions": []}
        ]},
        headers=headers,
    )
    assert resp.status_code == 409


def test_step_count_limits(client, make_program, supervisor):
    program = make_program(publish=False)
    headers = auth_headers(supervisor["token"])
    draft_id = program["draft_id"]

    too_many = [
        {"title": f"S{i}", "instruction": "x", "pass_condition": "y",
         "prerequisite_positions": []}
        for i in range(51)
    ]
    resp = client.put(
        f"/api/programs/versions/{draft_id}/steps",
        json={"steps": too_many}, headers=headers,
    )
    assert resp.status_code == 422

    resp = client.put(
        f"/api/programs/versions/{draft_id}/steps",
        json={"steps": []}, headers=headers,
    )
    assert resp.status_code == 422


# ---------- dependency validation ----------

def test_replace_steps_rejects_unknown_and_self_prerequisite(client, make_program, supervisor):
    program = make_program(publish=False)
    headers = auth_headers(supervisor["token"])
    draft_id = program["draft_id"]

    # unknown prerequisite
    resp = client.put(
        f"/api/programs/versions/{draft_id}/steps",
        json={"steps": [
            {"title": "S1", "instruction": "a", "pass_condition": "b",
             "prerequisite_positions": [2]},
        ]},
        headers=headers,
    )
    assert resp.status_code == 422

    # self prerequisite
    resp = client.put(
        f"/api/programs/versions/{draft_id}/steps",
        json={"steps": [
            {"title": "S1", "instruction": "a", "pass_condition": "b",
             "prerequisite_positions": [1]},
        ]},
        headers=headers,
    )
    assert resp.status_code == 422


def test_publish_rejects_cycle(client, make_program, supervisor):
    program = make_program(publish=False)
    headers = auth_headers(supervisor["token"])
    draft_id = program["draft_id"]

    # 1 -> 2 -> 1
    resp = client.put(
        f"/api/programs/versions/{draft_id}/steps",
        json={"steps": [
            {"title": "S1", "instruction": "a", "pass_condition": "b",
             "prerequisite_positions": [2]},
            {"title": "S2", "instruction": "a", "pass_condition": "b",
             "prerequisite_positions": [1]},
        ]},
        headers=headers,
    )
    assert resp.status_code == 422
    assert "cycle" in resp.json()["detail"]


def test_publish_rejects_longer_cycle(client, make_program, supervisor):
    program = make_program(publish=False)
    headers = auth_headers(supervisor["token"])
    draft_id = program["draft_id"]

    # 1 -> 2 -> 3 -> 4 -> 2
    resp = client.put(
        f"/api/programs/versions/{draft_id}/steps",
        json={"steps": [
            {"title": "S1", "instruction": "a", "pass_condition": "b",
             "prerequisite_positions": [2]},
            {"title": "S2", "instruction": "a", "pass_condition": "b",
             "prerequisite_positions": [3]},
            {"title": "S3", "instruction": "a", "pass_condition": "b",
             "prerequisite_positions": [4]},
            {"title": "S4", "instruction": "a", "pass_condition": "b",
             "prerequisite_positions": [2]},
        ]},
        headers=headers,
    )
    assert resp.status_code == 422


def test_enrollment_is_pinned_to_its_version(client, make_program, make_user, supervisor):
    """New publish / rollback must not change an in-flight learner's content."""
    program = make_program(capacity=5, steps=2)
    headers = auth_headers(supervisor["token"])
    learner = make_user("learner1", "learner")
    lh = auth_headers(learner["token"])

    resp = client.post(f"/api/programs/{program['id']}/enroll", headers=lh)
    assert resp.status_code == 201
    enrollment_v1 = resp.json()
    assert enrollment_v1["version_id"] == program["version_id"]

    # Supervisor publishes a new version with a different step set.
    resp = client.post(
        f"/api/programs/{program['id']}/drafts", json={}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    new_draft = resp.json()
    resp = client.put(
        f"/api/programs/versions/{new_draft['id']}/steps",
        json={"steps": [
            {"title": "Brand new", "instruction": "new content",
             "pass_condition": "new", "prerequisite_positions": []},
            {"title": "Brand new 2", "instruction": "new content",
             "pass_condition": "new", "prerequisite_positions": [1]},
            {"title": "Brand new 3", "instruction": "new content",
             "pass_condition": "new", "prerequisite_positions": [2]},
        ]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    resp = client.post(
        f"/api/programs/versions/{new_draft['id']}/publish", headers=headers
    )
    assert resp.status_code == 200, resp.text
    v2_id = resp.json()["id"]

    # Existing enrolment still points at v1...
    resp = client.get(
        f"/api/enrollments/{enrollment_v1['id']}", headers=lh
    )
    assert resp.json()["version_id"] == program["version_id"]

    # ...and a new enrolment pins v2.
    learner2 = make_user("learner2", "learner")
    resp = client.post(
        f"/api/programs/{program['id']}/enroll",
        headers=auth_headers(learner2["token"]),
    )
    assert resp.json()["version_id"] == v2_id

    # Rolling back to v1 changes new enrolments only.
    resp = client.post(
        f"/api/programs/{program['id']}/rollback/{program['version_id']}",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    learner3 = make_user("learner3", "learner")
    resp = client.post(
        f"/api/programs/{program['id']}/enroll",
        headers=auth_headers(learner3["token"]),
    )
    assert resp.json()["version_id"] == program["version_id"]
    # learner2 remains on v2
    resp = client.get("/api/my/enrollments", headers=auth_headers(learner2["token"]))
    pinned = [e for e in resp.json() if e["program_id"] == program["id"]]
    assert pinned[0]["version_id"] == v2_id
