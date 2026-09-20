"""Timeout escalation: retries and restarts never double-escalate."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.engine import process_due_escalations
from app.models import Task
from tests.factories import approve, publish, start

TIMEOUT_TEMPLATE = {
    "nodes": [
        {"id": "start", "type": "start", "next_node": "approve"},
        {"id": "approve", "type": "approval", "strategy": "any",
         "approvers": ["sleeper"], "next_node": "end",
         "timeout_seconds": 1, "escalation_target": "backup"},
        {"id": "end", "type": "end"},
    ]
}


async def _force_due(session_factory, instance_id):
    """Backdate due_at so the task looks timed out."""
    async with session_factory() as session:
        tasks = list(await session.scalars(select(Task)))
        for t in tasks:
            t.due_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        await session.commit()


@pytest.mark.asyncio
async def test_due_task_is_escalated_once(client, session_factory):
    await publish(client, "timeout", TIMEOUT_TEMPLATE)
    inst = await start(client, "timeout")
    await _force_due(session_factory, inst["id"])

    async with session_factory() as session:
        count = await process_due_escalations(session, batch_limit=20)
        await session.commit()
    assert count == 1

    detail = (await client.get(f"/instances/{inst['id']}")).json()
    assignees = sorted(t["assignee"] for t in detail["pending_tasks"])
    assert assignees == ["backup"]
    escalations = [e for e in detail["history"]
                   if e["event_type"] == "escalated"]
    assert len(escalations) == 1
    closed = [e for e in detail["history"]
              if e["event_type"] == "task_closed"]
    assert {e["detail"]["assignee"] for e in closed} == {"sleeper"}


@pytest.mark.asyncio
async def test_retry_processing_does_not_duplicate(client, session_factory):
    await publish(client, "timeout-retry", TIMEOUT_TEMPLATE)
    inst = await start(client, "timeout-retry")
    await _force_due(session_factory, inst["id"])

    for _ in range(3):  # worker retries on the same pending state
        async with session_factory() as session:
            await process_due_escalations(session, batch_limit=20)
            await session.commit()

    detail = (await client.get(f"/instances/{inst['id']}")).json()
    assert len([e for e in detail["history"]
                if e["event_type"] == "escalated"]) == 1
    backup_tasks = [t for t in detail["pending_tasks"]
                    if t["assignee"] == "backup"]
    assert len(backup_tasks) == 1


@pytest.mark.asyncio
async def test_restart_picks_up_unfinished_escalation(client, session_factory):
    """Simulate a restart: nothing processed before due time; after 'restart'
    (fresh engine call) the due task is handled."""
    await publish(client, "timeout-restart", TIMEOUT_TEMPLATE)
    inst = await start(client, "timeout-restart")
    await _force_due(session_factory, inst["id"])

    # first worker session (process starting up after downtime)
    async with session_factory() as session:
        assert await process_due_escalations(session, 20) == 1
        await session.commit()

    # nothing left to do after recovery
    async with session_factory() as session:
        assert await process_due_escalations(session, 20) == 0
        await session.commit()


@pytest.mark.asyncio
async def test_escalation_after_decision_is_noop(client, session_factory):
    await publish(client, "timeout-raced", TIMEOUT_TEMPLATE)
    inst = await start(client, "timeout-raced")
    vid = inst["template_version"]

    # approver acts just before the worker runs
    r = await approve(client, inst["id"], "sleeper", vid)
    assert r.status_code == 200

    await _force_due(session_factory, inst["id"])
    async with session_factory() as session:
        count = await process_due_escalations(session, 20)
        await session.commit()
    assert count == 0

    detail = (await client.get(f"/instances/{inst['id']}")).json()
    assert detail["status"] == "approved"
    assert all(
        e["event_type"] != "escalated" for e in detail["history"]
    )


@pytest.mark.asyncio
async def test_backup_assignee_can_approve_after_escalation(
    client, session_factory
):
    await publish(client, "timeout-backup", TIMEOUT_TEMPLATE)
    inst = await start(client, "timeout-backup")
    vid = inst["template_version"]
    await _force_due(session_factory, inst["id"])

    async with session_factory() as session:
        await process_due_escalations(session, 20)
        await session.commit()

    r = await approve(client, inst["id"], "backup", vid)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_original_assignee_cannot_act_after_escalation(
    client, session_factory
):
    await publish(client, "timeout-locked", TIMEOUT_TEMPLATE)
    inst = await start(client, "timeout-locked")
    vid = inst["template_version"]
    await _force_due(session_factory, inst["id"])

    async with session_factory() as session:
        await process_due_escalations(session, 20)
        await session.commit()

    # sleeper's todo was closed; only the backup can now decide
    r = await approve(client, inst["id"], "sleeper", vid)
    assert r.status_code == 409
    r = await approve(client, inst["id"], "backup", vid)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
