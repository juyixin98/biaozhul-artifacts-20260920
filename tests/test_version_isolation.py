"""Version isolation: immutability, pinning at start time, rollback semantics."""
from __future__ import annotations

from tests.helpers import create_published, decide, simple_chain_def, start


def test_instance_binds_version_at_creation(client):
    create_published(client, "v", simple_chain_def(assignees=("a1", "a2")))
    inst = start(client, "v")
    assert inst["version"] == 1
    assert {t["assignee"] for t in inst["open_tasks"]} == {"a1", "a2"}

    # publish v2 with a different approver set
    r = client.post(
        "/templates/v/versions",
        json={"definition": simple_chain_def(assignees=("b1", "b2"))},
    )
    assert r.status_code == 201
    r = client.post("/templates/v/versions/2/publish")
    assert r.status_code == 200

    inst2 = start(client, "v")
    assert inst2["version"] == 2
    assert {t["assignee"] for t in inst2["open_tasks"]} == {"b1", "b2"}


def test_running_instance_keeps_old_version_after_new_publish(client, req_id):
    create_published(client, "vi", simple_chain_def(assignees=("a1", "a2"), mode="any"))
    inst = start(client, "vi")
    iid = inst["id"]

    client.post(
        "/templates/vi/versions",
        json={"definition": simple_chain_def(assignees=("b1",), mode="any")},
    )
    client.post("/templates/vi/versions/2/publish")

    # old instance still has the v1 approver
    got = client.get(f"/instances/{iid}").json()
    assert got["version"] == 1
    assert {t["assignee"] for t in got["open_tasks"]} == {"a1", "a2"}

    r = decide(client, instance_id=iid, version=1, actor="a1",
               decision="approve", request_id=req_id())
    assert r.status_code == 200
    assert r.json()["instance"]["status"] == "completed"


def test_can_start_explicit_old_version_after_rollback(client):
    create_published(client, "rb", simple_chain_def(assignees=("a1",), mode="any"))
    client.post(
        "/templates/rb/versions",
        json={"definition": simple_chain_def(assignees=("b1",), mode="any")},
    )
    client.post("/templates/rb/versions/2/publish")
    assert client.get("/templates/rb").json()["current_version"] == 2

    # an instance on v2 stays on v2...
    inst_v2 = start(client, "rb")
    assert inst_v2["version"] == 2

    # ...roll back current to v1; only afterwards-created instances follow
    r = client.post("/templates/rb/rollback/1")
    assert r.status_code == 200
    assert r.json()["current_version"] == 1

    inst_new = start(client, "rb")
    assert inst_new["version"] == 1
    assert inst_new["open_tasks"][0]["assignee"] == "a1"

    got = client.get(f"/instances/{inst_v2['id']}").json()
    assert got["version"] == 2
    assert got["open_tasks"][0]["assignee"] == "b1"


def test_cannot_start_unpublished_version(client):
    create_published(client, "draft", simple_chain_def(mode="any", assignees=("a1",)))
    client.post(
        "/templates/draft/versions",
        json={"definition": simple_chain_def(mode="any", assignees=("b1",))},
    )
    r = client.post("/instances", json={"template_key": "draft", "submitter": "u", "version": 2})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "version_not_published"


def test_expected_version_mismatch_is_conflict_and_writes_no_history(client, req_id):
    create_published(client, "vc", simple_chain_def(mode="any", assignees=("a1",)))
    inst = start(client, "vc")
    before = client.get(f"/instances/{inst['id']}/history").json()["history"]

    r = decide(client, instance_id=inst["id"], version=99, actor="a1",
               decision="approve", request_id=req_id())
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "version_conflict"

    after = client.get(f"/instances/{inst['id']}/history").json()["history"]
    assert len(after) == len(before)


def test_rollback_to_unpublished_version_rejected(client):
    create_published(client, "rb2", simple_chain_def(mode="any", assignees=("a1",)))
    client.post(
        "/templates/rb2/versions",
        json={"definition": simple_chain_def(mode="any", assignees=("b1",))},
    )
    r = client.post("/templates/rb2/rollback/2")
    assert r.status_code == 409
