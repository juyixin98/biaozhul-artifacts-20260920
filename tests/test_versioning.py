"""Published templates are immutable; running instances stay on their version."""
import asyncio

import pytest

from tests.factories import (
    approve,
    branch_template,
    linear_template,
    publish,
    req_id,
    start,
)


@pytest.mark.asyncio
async def test_publish_appends_versions_and_running_instance_is_isolated(client):
    code = "isolation"
    v1 = await publish(client, code, linear_template(("u1",), "any"))
    assert v1["version"] == 1

    inst1 = await start(client, code)
    assert inst1["template_version"] == 1
    assert inst1["current_node_id"] == "approve"

    # publish a new, structurally different version (different approver)
    v2_def = linear_template(("u2",), "any")
    v2 = await publish(client, code, v2_def)
    assert v2["version"] == 2

    # new instance without explicit version -> newest
    inst2 = await start(client, code)
    assert inst2["template_version"] == 2

    # explicit old version binding works
    inst_old = await start(client, code, version=1)
    assert inst_old["template_version"] == 1

    # running v1 instance is still served by v1: u1 can act, u2 cannot
    resp = await approve(client, inst1["id"], "u2", expected_version=1)
    assert resp.status_code == 403
    resp = await approve(client, inst1["id"], "u1", expected_version=1)
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    # v2 instance expects u2
    resp = await approve(client, inst2["id"], "u2", expected_version=2)
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_rollback_creates_new_version_without_touching_running(client):
    code = "rollback"
    await publish(client, code, linear_template(("u1",), "any"))
    await publish(client, code, linear_template(("u2",), "any"))

    running = await start(client, code)  # v2, approver u2
    assert running["template_version"] == 2

    resp = await client.post(
        f"/templates/{code}/rollback/1",
        headers={"X-Request-Id": req_id()},
    )
    assert resp.status_code == 201, resp.text
    v3 = resp.json()
    assert v3["version"] == 3
    assert v3["definition"] == linear_template(("u1",), "any")

    # later instance uses rolled-back content (u1)
    later = await start(client, code)
    assert later["template_version"] == 3
    resp = await approve(client, later["id"], "u1", expected_version=3)
    assert resp.status_code == 200

    # running v2 instance keeps u2
    resp = await approve(client, running["id"], "u1", expected_version=2)
    assert resp.status_code == 403
    resp = await approve(client, running["id"], "u2", expected_version=2)
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_expected_version_conflict_writes_nothing(client):
    code = "version-conflict"
    await publish(client, code, linear_template(("u1",), "any"))
    inst = await start(client, code)

    resp = await approve(client, inst["id"], "u1", expected_version=99)
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "version_conflict"

    # instance untouched, task still pending
    detail = (await client.get(f"/instances/{inst['id']}")).json()
    assert detail["status"] == "running"
    assert len(detail["pending_tasks"]) == 1
    events = [e["event_type"] for e in detail["history"]]
    assert "approved" not in events


@pytest.mark.asyncio
async def test_branch_uses_pinned_version_definition(client):
    code = "branch-isolation"
    await publish(client, code, branch_template())
    big = await start(client, code, variables={"amount": 5000})
    small = await start(client, code, variables={"amount": 10})
    assert big["current_node_id"] == "big"
    assert small["current_node_id"] == "small"
    assert [t["assignee"] for t in big["pending_tasks"]] == ["bigboss"]
    assert [t["assignee"] for t in small["pending_tasks"]] == ["clerk"]


@pytest.mark.asyncio
async def test_concurrent_first_publishes_get_distinct_versions(client):
    code = "concurrent-publish"
    definition = linear_template(("u1",), "any")

    async def publish_one(rid):
        return await client.post(
            f"/templates/{code}/versions",
            json={"name": "t", "definition": definition},
            headers={"X-Request-Id": rid},
        )

    results = await asyncio.gather(
        publish_one(req_id()), publish_one(req_id()), publish_one(req_id())
    )
    assert sorted(r.status_code for r in results) == [201, 201, 201]
    versions = sorted(r.json()["version"] for r in results)
    assert versions == [1, 2, 3]

    listing = (await client.get(f"/templates/{code}/versions")).json()
    assert [v["version"] for v in listing] == [1, 2, 3]
