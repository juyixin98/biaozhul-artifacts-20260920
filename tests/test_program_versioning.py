"""Step dependency validation and version immutability."""
import pytest

from tests.conftest import auth, create_published_program, linear_steps, make_users


def test_simple_linear_program_publishes(client):
    sup, _ = make_users(client, 0)
    info = create_published_program(client, sup, linear_steps(3))
    v = info["version"]
    assert v["status"] == "published"
    assert v["version_number"] == 1
    assert len(v["content_digest"]) == 64
    assert [s["position"] for s in v["steps"]] == [1, 2, 3]


def test_unknown_prerequisite_rejected(client):
    sup, _ = make_users(client, 0)
    r = client.post("/programs", headers=auth(sup), json={"title": "P"})
    pid = r.json()["id"]
    steps = [
        {"key": "a", "position": 1, "instruction": "i", "pass_condition": "c",
         "prerequisite_keys": ["ghost"]},
    ]
    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(sup),
        json={"steps": steps, "publish": True},
    )
    assert r.status_code == 422
    assert "unknown step" in r.json()["error"]["message"]


def test_self_dependency_rejected(client):
    sup, _ = make_users(client, 0)
    r = client.post("/programs", headers=auth(sup), json={"title": "P"})
    pid = r.json()["id"]
    steps = [
        {"key": "a", "position": 1, "instruction": "i", "pass_condition": "c",
         "prerequisite_keys": ["a"]},
    ]
    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(sup),
        json={"steps": steps, "publish": True},
    )
    assert r.status_code == 422


def test_forward_prerequisite_rejected(client):
    sup, _ = make_users(client, 0)
    r = client.post("/programs", headers=auth(sup), json={"title": "P"})
    pid = r.json()["id"]
    steps = [
        {"key": "a", "position": 1, "instruction": "i", "pass_condition": "c",
         "prerequisite_keys": ["b"]},
        {"key": "b", "position": 2, "instruction": "i", "pass_condition": "c",
         "prerequisite_keys": []},
    ]
    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(sup),
        json={"steps": steps, "publish": True},
    )
    assert r.status_code == 422
    assert "earlier steps" in r.json()["error"]["message"]


def test_diamond_dag_is_allowed(client):
    sup, _ = make_users(client, 0)
    r = client.post("/programs", headers=auth(sup), json={"title": "P"})
    pid = r.json()["id"]
    # a -> b, a -> c, b & c -> d
    steps = [
        {"key": "a", "position": 1, "instruction": "i", "pass_condition": "c",
         "prerequisite_keys": []},
        {"key": "b", "position": 2, "instruction": "i", "pass_condition": "c",
         "prerequisite_keys": ["a"]},
        {"key": "c", "position": 3, "instruction": "i", "pass_condition": "c",
         "prerequisite_keys": ["a"]},
        {"key": "d", "position": 4, "instruction": "i", "pass_condition": "c",
         "prerequisite_keys": ["b", "c"]},
    ]
    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(sup),
        json={"steps": steps, "publish": True},
    )
    assert r.status_code == 201, r.text


def test_cyclic_dependencies_rejected(client):
    """Earlier-position ordering makes direct cycles impossible via a single
    forward edge, but validation must still defend the generic graph check.
    We exercise the cycle detector directly to prove it works."""
    from app.services.programs import _assert_acyclic
    from app.errors import ValidationError

    by_key = {
        "a": {"key": "a", "position": 1, "prerequisite_keys": []},
        "b": {"key": "b", "position": 2, "prerequisite_keys": ["a"]},
    }
    # Artificially create a cycle (defence in depth).
    by_key["a"]["prerequisite_keys"] = ["b"]
    with pytest.raises(ValidationError, match="cycle"):
        _assert_acyclic(by_key)


def test_more_than_50_steps_rejected(client):
    sup, _ = make_users(client, 0)
    r = client.post("/programs", headers=auth(sup), json={"title": "P"})
    pid = r.json()["id"]
    steps = linear_steps(50)
    steps.append(
        {"key": "too_many", "position": 51, "instruction": "i",
         "pass_condition": "c", "prerequisite_keys": ["s50"]}
    )
    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(sup),
        json={"steps": steps, "publish": True},
    )
    # Rejected at request validation (max_length) — still a 422.
    assert r.status_code == 422

    # Exactly 50 steps is accepted by both layers.
    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(sup),
        json={"steps": linear_steps(50), "publish": True},
    )
    assert r.status_code == 201, r.text


def test_non_consecutive_positions_rejected(client):
    sup, _ = make_users(client, 0)
    r = client.post("/programs", headers=auth(sup), json={"title": "P"})
    pid = r.json()["id"]
    steps = [
        {"key": "a", "position": 1, "instruction": "i", "pass_condition": "c"},
        {"key": "b", "position": 3, "instruction": "i", "pass_condition": "c",
         "prerequisite_keys": ["a"]},
    ]
    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(sup),
        json={"steps": steps, "publish": True},
    )
    assert r.status_code == 422


def test_published_version_is_immutable_and_changes_make_new_version(client):
    sup, _ = make_users(client, 0)
    info = create_published_program(client, sup, linear_steps(2))
    pid = info["program_id"]
    v1 = info["version"]

    # Publishing an already-published version is rejected.
    r = client.post(
        f"/programs/versions/{v1['id']}/publish", headers=auth(sup)
    )
    assert r.status_code == 409

    # Any modification must go through a new version.
    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(sup),
        json={"steps": linear_steps(3), "publish": True},
    )
    assert r.status_code == 201
    v2 = r.json()
    assert v2["version_number"] == 2
    assert v2["id"] != v1["id"]
    assert v2["content_digest"] != v1["content_digest"]

    # v1 stays published and unchanged.
    r = client.get(f"/programs/versions/{v1['id']}")
    assert r.json()["status"] == "published"
    assert len(r.json()["steps"]) == 2

    # Program now points at v2.
    r = client.get(f"/programs/{pid}")
    assert r.json()["current_version_id"] == v2["id"]


def test_course_requires_published_version(client):
    sup, _ = make_users(client, 0)
    r = client.post("/programs", headers=auth(sup), json={"title": "P"})
    pid = r.json()["id"]
    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(sup),
        json={"steps": linear_steps(2), "publish": False},
    )
    draft = r.json()
    assert draft["status"] == "draft"


    r = client.post(
        "/courses",
        headers=auth(sup),
        json={
            "title": "C-draft",
            "version_id": draft["id"],
            "capacity": 5,
            "enroll_deadline": "2035-01-01T00:00:00+00:00",
        },
    )
    assert r.status_code == 422

    # Publish it, then course creation works.
    r = client.post(f"/programs/versions/{draft['id']}/publish", headers=auth(sup))
    assert r.status_code == 200
    r = client.post(
        "/courses",
        headers=auth(sup),
        json={
            "title": "C",
            "version_id": draft["id"],
            "capacity": 5,
            "enroll_deadline": "2035-01-01T00:00:00+00:00",
        },
    )
    assert r.status_code == 201


def test_rollback_points_program_at_old_version(client):
    sup, _ = make_users(client, 0)
    info = create_published_program(client, sup, linear_steps(2))
    pid = info["program_id"]
    v1 = info["version"]

    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(sup),
        json={"steps": linear_steps(3), "publish": True},
    )
    r.json()

    r = client.post(
        f"/programs/{pid}/rollback",
        headers=auth(sup),
        json={"version_id": v1["id"]},
    )
    assert r.status_code == 200
    assert r.json()["current_version_id"] == v1["id"]
