"""Workflow engine: all state transitions live here.

Every mutation follows the same discipline:

* ``SELECT ... FOR UPDATE`` on the instance row serialises concurrent
  decisions / withdrawals / escalations for the same instance.
* Instance status, task rows and audit events are flushed in one session and
  committed by the caller inside the *same* transaction as the idempotency
  record.  Hence either the whole transition happens or none of it does.
* At most one effective transition is possible: once the instance leaves
  ``running`` (or leaves the approval node), later conflicting requests hit a
  state guard and write nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import (
    ACTION_REJECT,
    AUDIT_APPROVED,
    AUDIT_ENTER_NODE,
    AUDIT_ESCALATED,
    AUDIT_REJECTED,
    AUDIT_STARTED,
    AUDIT_TASK_CLOSED,
    AUDIT_TEMPLATE_PUBLISHED,
    AUDIT_TEMPLATE_ROLLED_BACK,
    AUDIT_WITHDRAWN,
    INSTANCE_APPROVED,
    INSTANCE_REJECTED,
    INSTANCE_RUNNING,
    INSTANCE_WITHDRAWN,
    NODE_APPROVAL,
    NODE_CONDITION,
    NODE_END,
    NODE_START,
    SIGN_ALL,
    SIGN_ANY,
    TASK_APPROVED,
    TASK_CLOSED,
    TASK_PENDING,
    TASK_REJECTED,
    TASK_WITHDRAWN,
)
from app.errors import bad_request, conflict, forbidden, not_found
from app.expressions import evaluate
from app.idempotency import acquire_template_lock
from app.models import AuditEvent, Instance, Task, TemplateVersion
from app.validation import validate_definition


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

async def publish_template(
    session: AsyncSession,
    template_code: str,
    name: str,
    definition: dict[str, Any],
    actor: str,
) -> TemplateVersion:
    """Validate and append a new immutable version."""
    issues = validate_definition(definition)
    if issues:
        raise bad_request(
            "invalid_template",
            "; ".join(f"[{i.code}] {i.message}" for i in issues),
        )

    await acquire_template_lock(session, template_code)
    latest = await session.scalar(
        select(func.max(TemplateVersion.version)).where(
            TemplateVersion.template_code == template_code
        )
    )
    version = (latest or 0) + 1
    record = TemplateVersion(
        template_code=template_code,
        version=version,
        name=name,
        definition=definition,
        created_by=actor,
    )
    session.add(record)
    session.add(
        AuditEvent(
            template_code=template_code,
            template_version=version,
            event_type=AUDIT_TEMPLATE_PUBLISHED,
            actor=actor,
            detail={"name": name},
        )
    )
    await session.flush()
    return record


async def rollback_template(
    session: AsyncSession, template_code: str, target_version: int, actor: str
) -> TemplateVersion:
    """Roll back by *appending* a copy of an older version.

    Running instances keep their pinned version; only instances created later
    see the rolled-back content.
    """
    source = await session.get(TemplateVersion, (template_code, target_version))
    if source is None:
        raise not_found(f"template {template_code} version {target_version} not found")

    await acquire_template_lock(session, template_code)
    latest = await session.scalar(
        select(func.max(TemplateVersion.version)).where(
            TemplateVersion.template_code == template_code
        )
    )
    new_version = (latest or 0) + 1
    record = TemplateVersion(
        template_code=template_code,
        version=new_version,
        name=source.name,
        definition=source.definition,
        created_by=actor,
    )
    session.add(record)
    session.add(
        AuditEvent(
            template_code=template_code,
            template_version=new_version,
            event_type=AUDIT_TEMPLATE_ROLLED_BACK,
            actor=actor,
            detail={"restored_from_version": target_version},
        )
    )
    await session.flush()
    return record


# --------------------------------------------------------------------------- #
# Instance lifecycle / traversal
# --------------------------------------------------------------------------- #

def _audit(
    session: AsyncSession,
    event_type: str,
    *,
    instance_id: int | None = None,
    template_code: str | None = None,
    template_version: int | None = None,
    node_id: str | None = None,
    actor: str | None = None,
    detail: dict | None = None,
) -> None:
    session.add(
        AuditEvent(
            instance_id=instance_id,
            template_code=template_code,
            template_version=template_version,
            event_type=event_type,
            node_id=node_id,
            actor=actor,
            detail=detail or {},
        )
    )


async def start_instance(
    session: AsyncSession,
    template_code: str,
    requested_version: int | None,
    business_key: str,
    variables: dict[str, Any],
    submitter: str,
) -> Instance:
    if requested_version is None:
        template = await session.scalar(
            select(TemplateVersion)
            .where(TemplateVersion.template_code == template_code)
            .order_by(TemplateVersion.version.desc())
            .limit(1)
        )
    else:
        template = await session.get(
            TemplateVersion, (template_code, requested_version)
        )
    if template is None:
        raise not_found(
            f"published template {template_code}"
            + (f" version {requested_version}" if requested_version else "")
            + " not found"
        )

    instance = Instance(
        template_code=template.template_code,
        template_version=template.version,
        business_key=business_key,
        variables=variables or {},
        status=INSTANCE_RUNNING,
        submitter=submitter,
    )
    session.add(instance)
    await session.flush()
    _audit(
        session,
        AUDIT_STARTED,
        instance_id=instance.id,
        template_code=template.template_code,
        template_version=template.version,
        actor=submitter,
        detail={"business_key": business_key, "variables": instance.variables},
    )
    start_id = _find_node_id(template.definition, NODE_START)
    await _enter_node(session, instance, template.definition, start_id)
    await session.flush()
    # reload with tasks eagerly loaded for serialization
    instance = await session.scalar(
        select(Instance)
        .where(Instance.id == instance.id)
        .options(selectinload(Instance.tasks))
    )
    return instance


def _find_node_id(definition: dict, node_type: str) -> str:
    for node in definition["nodes"]:
        if node["type"] == node_type:
            return node["id"]
    raise RuntimeError(f"no {node_type} node (template was validated at publish)")


def _node(definition: dict, node_id: str) -> dict:
    for node in definition["nodes"]:
        if node["id"] == node_id:
            return node
    raise RuntimeError(f"node {node_id} missing (template was validated at publish)")


async def _enter_node(
    session: AsyncSession, instance: Instance, definition: dict, node_id: str
) -> None:
    """Walk start/condition nodes automatically; stop for approval/end."""
    current = node_id
    while True:
        node = _node(definition, current)
        instance.current_node_id = node["id"]
        _audit(
            session,
            AUDIT_ENTER_NODE,
            instance_id=instance.id,
            template_code=instance.template_code,
            template_version=instance.template_version,
            node_id=node["id"],
            detail={"type": node["type"]},
        )
        ntype = node["type"]
        if ntype == NODE_START:
            current = node["next_node"]
            continue
        if ntype == NODE_CONDITION:
            chosen = _select_branch(node, instance.variables)
            if chosen is None:
                raise conflict(
                    "no_matching_branch",
                    f"no branch matched at condition node {node['id']!r} "
                    "and no default is defined",
                )
            current = chosen
            continue
        if ntype == NODE_END:
            instance.status = INSTANCE_APPROVED
            return
        # approval node: create one pending task per approver
        due_at = None
        if node.get("timeout_seconds"):
            due_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(node["timeout_seconds"])
            )
        for approver in node["approvers"]:
            session.add(
                Task(
                    instance_id=instance.id,
                    node_id=node["id"],
                    assignee=approver,
                    status=TASK_PENDING,
                    sign_strategy=node["strategy"],
                    due_at=due_at,
                    escalation_target=node.get("escalation_target"),
                )
            )
        return


def _select_branch(node: dict, variables: dict) -> str | None:
    for branch in node["branches"]:
        try:
            if evaluate(branch["expression"], variables):
                return branch["next_node"]
        except Exception:  # safety net; malformed exprs are rejected at publish
            continue
    return node.get("default")


# --------------------------------------------------------------------------- #
# Decisions (approve / reject)
# --------------------------------------------------------------------------- #

async def decide(
    session: AsyncSession,
    instance_id: int,
    expected_version: int,
    actor: str,
    action: str,
    comment: str | None,
) -> tuple[Instance, list[Task]]:
    instance = await _locked_instance(session, instance_id, with_tasks=True)
    if instance.template_version != expected_version:
        raise conflict(
            "version_conflict",
            f"instance runs version {instance.template_version}, "
            f"request expected {expected_version}",
        )
    if instance.status != INSTANCE_RUNNING:
        raise conflict(
            "instance_not_running",
            f"instance is {instance.status}; no further decisions accepted",
        )

    node_tasks = [
        t for t in instance.tasks if t.node_id == instance.current_node_id
    ]
    pending = [t for t in node_tasks if t.status == TASK_PENDING]
    if not node_tasks or all(t.status == TASK_CLOSED for t in node_tasks):
        raise conflict("no_pending_task", "no pending task at the current node")
    # Authorize against the user's task at *this node* regardless of its state:
    # an outsider is 403, while the legitimate assignee whose task is already
    # decided (e.g. a duplicated request) gets the state conflict below.
    if not any(t.assignee == actor for t in node_tasks):
        raise forbidden("you are not an assignee of the current pending task")
    if not pending:
        raise conflict(
            "no_pending_task",
            "the task at the current node is already decided",
        )
    mine = [t for t in pending if t.assignee == actor]
    if not mine:
        # assignee exists at this node but their own task is already decided
        raise conflict(
            "task_already_decided",
            "your task at this node is already decided",
        )

    definition = await _definition(session, instance)
    node = _node(definition, instance.current_node_id)
    assert node["type"] == NODE_APPROVAL
    now = datetime.now(timezone.utc)

    if action == ACTION_REJECT:
        # Explicit rejection is decisive for both strategies: the instance
        # terminates immediately and every other pending todo is closed.
        my_task = mine[0]
        my_task.status = TASK_REJECTED
        my_task.decided_by = actor
        my_task.comment = comment
        my_task.decided_at = now
        for other in pending:
            if other.id != my_task.id:
                other.status = TASK_CLOSED
        _close_audit(session, instance, pending, my_task.id, actor)
        instance.status = INSTANCE_REJECTED
        instance.reject_reason = comment or "rejected"
        _audit(
            session,
            AUDIT_REJECTED,
            instance_id=instance.id,
            template_code=instance.template_code,
            template_version=instance.template_version,
            node_id=node["id"],
            actor=actor,
            detail={"task_id": my_task.id, "comment": comment},
        )
        return await _reload(session, instance), [t for t in pending]

    # action == approve
    if node["strategy"] == SIGN_ANY:
        my_task = mine[0]
        my_task.status = TASK_APPROVED
        my_task.decided_by = actor
        my_task.comment = comment
        my_task.decided_at = now
        closed: list[Task] = []
        for other in pending:
            if other.id != my_task.id:
                other.status = TASK_CLOSED
                closed.append(other)
        _close_audit(session, instance, closed, my_task.id, actor)
        _audit(
            session,
            AUDIT_APPROVED,
            instance_id=instance.id,
            template_code=instance.template_code,
            template_version=instance.template_version,
            node_id=node["id"],
            actor=actor,
            detail={"task_id": my_task.id, "strategy": SIGN_ANY, "comment": comment},
        )
        await _enter_node(session, instance, definition, node["next_node"])
        return await _reload(session, instance), pending

    # 全签: every approver must approve; one rejection terminates (handled
    # above).  Each pending task can only be approved once (status guard), so
    # duplicate/concurrent approvals by the same user produce no second
    # transition.
    my_task = mine[0]
    my_task.status = TASK_APPROVED
    my_task.decided_by = actor
    my_task.comment = comment
    my_task.decided_at = now
    _audit(
        session,
        AUDIT_APPROVED,
        instance_id=instance.id,
        template_code=instance.template_code,
        template_version=instance.template_version,
        node_id=node["id"],
        actor=actor,
        detail={"task_id": my_task.id, "strategy": SIGN_ALL, "comment": comment},
    )
    still_pending = [
        t for t in instance.tasks
        if t.node_id == node["id"] and t.status == TASK_PENDING
    ]
    if not still_pending:
        await _enter_node(session, instance, definition, node["next_node"])
    return await _reload(session, instance), [
        t for t in instance.tasks if t.node_id == node["id"]
    ]


def _close_audit(
    session: AsyncSession,
    instance: Instance,
    closed: list[Task],
    deciding_task_id: int,
    actor: str,
) -> None:
    for task in closed:
        if task.id == deciding_task_id:
            continue
        _audit(
            session,
            AUDIT_TASK_CLOSED,
            instance_id=instance.id,
            template_code=instance.template_code,
            template_version=instance.template_version,
            node_id=task.node_id,
            actor=actor,
            detail={"task_id": task.id, "assignee": task.assignee},
        )


async def withdraw(
    session: AsyncSession, instance_id: int, actor: str, comment: str | None
) -> Instance:
    instance = await _locked_instance(session, instance_id, with_tasks=True)
    if instance.submitter != actor:
        raise forbidden("only the submitter may withdraw this instance")
    if instance.status != INSTANCE_RUNNING:
        raise conflict(
            "instance_not_running",
            f"instance is {instance.status}; cannot withdraw",
        )
    for task in instance.tasks:
        if (
            task.node_id == instance.current_node_id
            and task.status == TASK_PENDING
        ):
            task.status = TASK_WITHDRAWN
            task.decided_by = actor
            task.comment = comment
            task.decided_at = datetime.now(timezone.utc)
    instance.status = INSTANCE_WITHDRAWN
    _audit(
        session,
        AUDIT_WITHDRAWN,
        instance_id=instance.id,
        template_code=instance.template_code,
        template_version=instance.template_version,
        node_id=instance.current_node_id,
        actor=actor,
        detail={"comment": comment},
    )
    return await _reload(session, instance)


# --------------------------------------------------------------------------- #
# Timeout escalation
# --------------------------------------------------------------------------- #

def due_tasks_stmt(batch_limit: int) -> Select:
    return (
        select(Task)
        .where(
            Task.status == TASK_PENDING,
            Task.escalated.is_(False),
            Task.escalation_target.is_not(None),
            Task.due_at.is_not(None),
            Task.due_at <= datetime.now(timezone.utc),
        )
        .order_by(Task.due_at)
        .limit(batch_limit)
    )


async def process_due_escalations(
    session: AsyncSession, batch_limit: int
) -> int:
    """Escalate all due tasks. Returns the number of escalated tasks.

    Safe to retry and to run after a restart: a task is only escalated when it
    is still pending at a running instance and ``escalated`` is still False,
    all checked under the instance row lock, so the transition fires once even
    if workers race or a previous attempt died mid-way.
    """
    due = list(await session.scalars(due_tasks_stmt(batch_limit)))
    count = 0
    for task in due:
        instance = await _locked_instance(
            session, task.instance_id, with_tasks=True
        )
        # re-read task state under the lock (another process may have acted)
        fresh = await session.get(Task, task.id)
        if (
            fresh.escalated
            or fresh.status != TASK_PENDING
            or instance.status != INSTANCE_RUNNING
            or fresh.node_id != instance.current_node_id
            or not fresh.escalation_target
        ):
            continue
        # Timeout decision: the overdue todos are all closed and a single new
        # todo is opened for the escalation target.  Leaving the old todos
        # pending would let the original assignee approve in parallel with the
        # backup (ambiguous under 全签); closing them makes the backup the sole
        # decider.  Closure itself signals the transition, so it fires once.
        node_tasks = [
            t for t in instance.tasks
            if t.node_id == fresh.node_id and t.status == TASK_PENDING
        ]
        for stale in node_tasks:
            stale.status = TASK_CLOSED
            stale.escalated = True
            _audit(
                session,
                AUDIT_TASK_CLOSED,
                instance_id=instance.id,
                template_code=instance.template_code,
                template_version=instance.template_version,
                node_id=stale.node_id,
                detail={
                    "task_id": stale.id,
                    "assignee": stale.assignee,
                    "reason": "timeout_escalation",
                },
            )
        fresh.escalated = True
        session.add(
            Task(
                instance_id=instance.id,
                node_id=fresh.node_id,
                assignee=fresh.escalation_target,
                status=TASK_PENDING,
                sign_strategy=fresh.sign_strategy,
                due_at=None,
                escalation_target=None,
            )
        )
        _audit(
            session,
            AUDIT_ESCALATED,
            instance_id=instance.id,
            template_code=instance.template_code,
            template_version=instance.template_version,
            node_id=fresh.node_id,
            detail={
                "from_task_id": fresh.id,
                "from_assignee": fresh.assignee,
                "to_assignee": fresh.escalation_target,
            },
        )
        count += 1
    if count:
        await session.flush()
    return count


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

async def _reload(session: AsyncSession, instance: Instance) -> Instance:
    """Fresh aggregate load with tasks eagerly populated (safe to serialize)."""
    await session.flush()
    return await session.scalar(
        select(Instance)
        .where(Instance.id == instance.id)
        .options(selectinload(Instance.tasks))
    )


async def _locked_instance(
    session: AsyncSession, instance_id: int, with_tasks: bool = False
) -> Instance:
    stmt = select(Instance).where(Instance.id == instance_id).with_for_update()
    if with_tasks:
        stmt = stmt.options(selectinload(Instance.tasks))
    instance = await session.scalar(stmt)
    if instance is None:
        raise not_found(f"instance {instance_id} not found")
    return instance


async def _definition(session: AsyncSession, instance: Instance) -> dict:
    template = await session.get(
        TemplateVersion, (instance.template_code, instance.template_version)
    )
    if template is None:
        raise RuntimeError("pinned template version disappeared")
    return template.definition
