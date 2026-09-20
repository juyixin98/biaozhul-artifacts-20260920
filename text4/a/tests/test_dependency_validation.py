"""Dependency-graph validation, publish gating and immutability."""
from tests.factories import create_published_program, headers, learner, make_steps, manager


def _create_version(client, mgr_id, program_id, steps):
    return client.post(
        f"/programs/{program_id}/versions", json={"steps": steps}, headers=headers(mgr_id)
    )


def test_valid_chain_and_diamond_publish(client):
    mgr = manager(client)
    pid, vid = create_published_program(client, mgr, make_steps([], [1], [1, 2]))
    body = client.get(f"/versions/{vid}").json()
    assert body["status"] == "PUBLISHED"
    assert body["steps"][2]["prerequisite_orders"] == [1, 2]


def test_dangling_prerequisite_rejected(client):
    mgr = manager(client)
    pid = client.post("/programs", json={"title": "P"}, headers=headers(mgr)).json()["id"]
    resp = _create_version(client, mgr, pid, make_steps([], [3]))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PREREQUISITE_UNKNOWN"


def test_self_prerequisite_rejected(client):
    mgr = manager(client)
    pid = client.post("/programs", json={"title": "P"}, headers=headers(mgr)).json()["id"]
    resp = _create_version(client, mgr, pid, make_steps([1]))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PREREQUISITE_SELF"


def test_cycle_rejected(client):
    mgr = manager(client)
    pid = client.post("/programs", json={"title": "P"}, headers=headers(mgr)).json()["id"]
    # 1 -> 2 -> 3 -> 1
    resp = _create_version(client, mgr, pid, make_steps([3], [1], [2]))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "STEPS_CYCLIC"


def test_self_cycle_of_two_rejected(client):
    mgr = manager(client)
    pid = client.post("/programs", json={"title": "P"}, headers=headers(mgr)).json()["id"]
    resp = _create_version(client, mgr, pid, make_steps([2], [1]))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "STEPS_CYCLIC"


def test_too_many_steps_rejected(client):
    mgr = manager(client)
    pid = client.post("/programs", json={"title": "P"}, headers=headers(mgr)).json()["id"]
    steps = make_steps(*[[] for _ in range(51)])
    resp = _create_version(client, mgr, pid, steps)
    assert resp.status_code == 422


def test_fifty_steps_allowed(client):
    mgr = manager(client)
    pid = client.post("/programs", json={"title": "P"}, headers=headers(mgr)).json()["id"]
    # Every step depends on the previous one: valid long chain.
    steps = make_steps(*[[]] + [[i] for i in range(1, 50)])
    resp = _create_version(client, mgr, pid, steps)
    assert resp.status_code == 201, resp.text


def test_publish_is_idempotent_conflict_and_version_immutable(client):
    mgr = manager(client)
    _, vid = create_published_program(client, mgr)
    resp = client.post(f"/versions/{vid}/publish", headers=headers(mgr))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERSION_ALREADY_PUBLISHED"


def test_edit_creates_new_version_with_distinct_content(client):
    mgr = manager(client)
    pid, v1 = create_published_program(client, mgr, make_steps([], [1]))
    v2 = _create_version(client, mgr, pid, make_steps([], [1], [1, 2]))
    assert v2.status_code == 201
    v2_body = v2.json()
    assert v2_body["version"] == 2
    assert v2_body["status"] == "DRAFT"

    # v1 content is untouched.
    v1_body = client.get(f"/versions/{v1}").json()
    assert len(v1_body["steps"]) == 2
    client.post(f"/versions/{v2_body['id']}/publish", headers=headers(mgr))
    program = client.get(f"/programs/{pid}").json()
    assert program["current_version_id"] == v2_body["id"]


def test_course_requires_published_version(client):
    mgr = manager(client)
    pid = client.post("/programs", json={"title": "P"}, headers=headers(mgr)).json()["id"]
    vid = _create_version(client, mgr, pid, make_steps([])).json()["id"]
    resp = client.post(
        "/courses",
        json={
            "version_id": vid,
            "capacity": 3,
            "enrollment_deadline": "2030-01-01T00:00:00+00:00",
        },
        headers=headers(mgr),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VERSION_NOT_PUBLISHED"


def test_rollback_only_accepts_published(client):
    mgr = manager(client)
    pid = client.post("/programs", json={"title": "P"}, headers=headers(mgr)).json()["id"]
    v1 = _create_version(client, mgr, pid, make_steps([])).json()["id"]
    resp = client.put(
        f"/programs/{pid}/current-version",
        json={"version_id": v1},
        headers=headers(mgr),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VERSION_NOT_PUBLISHED"


def test_learner_cannot_author_programs(client):
    l = learner(client)
    resp = client.post("/programs", json={"title": "P"}, headers=headers(l))
    assert resp.status_code == 403
