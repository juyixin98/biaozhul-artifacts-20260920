"""Runtime workflow engine: instance lifecycle, decisions, withdraw, escalation.

Concurrency model
-----------------
Every state transition takes a row lock on the instance first
(``SELECT ... FOR UPDATE``), so concurrent approve/reject/withdraw/escalate
calls on the same instance serialize. Task rows are locked additionally.
State, todos, history and the escalation queue are committed in one
transaction, hence at most one effective transition per activation.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import catalog
from .errors import DomainError
from .expression import ExpressionError, evaluate
from .models import (
    ApprovalRequest,
    HistoryRecord,
    Instance,
    ScheduledEscalation,
    Task,
    utcnow,
)
from .schemas import DecisionIn, InstanceStart, WithdrawIn

# --------------------------------------------------------------------------- #
# Definition helpers (operate on the immutable snapshot)
# --------------------------------------------------------------------------- #


def _index_definition(snapshot: dict) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    nodes = {n["id"]: n for n in snapshot["nodes"]}
    outgoing: dict[str, list[dict]] = {}
    for edge in snapshot["edges"]:
        outgoing.setdefault(edge["source"], []).append(edge)
    return nodes, outgoing


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def _add_history(db: Session, instance: Instance, action: str, *, node_id=None, actor=None,
                 detail=None, comment=None) -> HistoryRecord:
    max_seq = db.scalar(
        select(func.coalesce(func.max(HistoryRecord.seq), 0)).where(
            HistoryRecord.instance_id == instance.id
        )
    )
    record = HistoryRecord(
        instance_id=instance.id,
        seq=max_seq + 1,
        action=action,
        node_id=node_id,
        actor=actor,
        detail=detail or {},
        comment=comment,
    )
    db.add(record)
    return record


# --------------------------------------------------------------------------- #
# Instance creation / graph traversal
# --------------------------------------------------------------------------- #


def start_instance(db: Session, body: InstanceStart) -> Instance:
    template, version = catalog.get_published_version(db, body.template_key, body.version)

    instance = Instance(
        template_id=template.id,
        template_version_id=version.id,
        version_number=version.version,
        definition_snapshot=version.definition,
        business_key=body.business_key,
        submitter=body.submitter,
        payload=body.payload,
        status="running",
    )
    db.add(instance)
    db.flush()
    _add_history(
        db,
        instance,
        "instance_started",
        actor=body.submitter,
        detail={"template_key": template.key, "version": version.version},
    )
    _walk(db, instance)
    db.commit()
    db.refresh(instance)
    return instance


def _walk(db: Session, instance: Instance) -> None:
    """Advance from the current node through start/condition nodes until an
    approval node opens, an end node is reached, or the flow stops."""
    nodes, outgoing = _index_definition(instance.definition_snapshot)

    cur_id = instance.current_node_id
    if cur_id is None:
        cur_id = next(n["id"] for n in instance.definition_snapshot["nodes"] if n["type"] == "start")

    while True:
        node = nodes[cur_id]
        instance.current_node_id = cur_id
        _add_history(db, instance, "node_entered", node_id=cur_id, detail={"type": node["type"]})

        ntype = node["type"]

        if ntype == "end":
            instance.status = "completed" if node["terminal"] == "approved" else "rejected"
            if instance.status == "rejected":
                instance.reject_reason = instance.reject_reason or "流程到达驳回结束节点"
            instance.current_node_id = None
            instance.finished_at = utcnow()
            _add_history(
                db,
                instance,
                "instance_completed" if instance.status == "completed" else "instance_rejected",
                node_id=cur_id,
                detail={"terminal": node["terminal"]},
            )
            return

        if ntype == "approval":
            _open_approval(db, instance, node, generation=0)
            return

        # start and approval nodes have exactly one edge; condition branches.
        edges = outgoing.get(cur_id, [])
        if ntype == "start":
            cur_id = edges[0]["target"]
            continue

        # condition: first expression that matches, else the default edge.
        chosen = None
        for edge in edges:
            if edge["expression"] is None:
                continue
            try:
                if evaluate(edge["expression"], instance.payload):
                    chosen = edge
                    break
            except ExpressionError as exc:
                raise DomainError(
                    "condition_evaluation_failed",
                    f"expression on edge {cur_id!r} failed: {exc}",
                    500,
                )
        if chosen is None:
            chosen = next((e for e in edges if e["expression"] is None), None)
        if chosen is None:  # validated at publish time; defensive only
            raise DomainError("no_branch_matched", f"no branch matched at condition {cur_id!r}", 500)
        _add_history(
            db, instance, "branch_taken", node_id=cur_id,
            detail={"target": chosen["target"], "expression": chosen["expression"]},
        )
        cur_id = chosen["target"]


def _next_generation(db: Session, instance: Instance, node_id: str) -> int:
    max_gen = db.scalar(
        select(func.coalesce(func.max(Task.generation), -1)).where(
            Task.instance_id == instance.id, Task.node_id == node_id
        )
    )
    return max_gen + 1


def _open_approval(db: Session, instance: Instance, node: dict, generation: int,
                   assignees: list[str] | None = None) -> None:
    """Create one pending task per distinct assignee and arm the escalation."""
    assignee_list = list(dict.fromkeys(assignees if assignees is not None else node["assignees"]))
    for assignee in assignee_list:
        db.add(
            Task(
                instance_id=instance.id,
                node_id=node["id"],
                generation=generation,
                assignee=assignee,
                status="pending",
            )
        )
    _add_history(
        db,
        instance,
        "approval_opened",
        node_id=node["id"],
        detail={
            "mode": node["mode"],
            "assignees": assignee_list,
            "generation": generation,
        },
    )
    # Escalation is armed only for the original generation. An escalated
    # generation has escalate_to as its assignees and is not armed again,
    # keeping the transition one-shot.
    if generation == 0:
        timeout = node.get("timeout_seconds")
        escalate_to = node.get("escalate_to")
        if timeout and escalate_to:
            db.add(
                ScheduledEscalation(
                    instance_id=instance.id,
                    node_id=node["id"],
                    generation=generation,
                    due_at=utcnow() + timedelta(seconds=timeout),
                    done=False,
                )
            )


def _cancel_escalations(db: Session, instance: Instance, node_id: str) -> None:
    rows = db.scalars(
        select(ScheduledEscalation).where(
            ScheduledEscalation.instance_id == instance.id,
            ScheduledEscalation.node_id == node_id,
            ScheduledEscalation.done.is_(False),
        )
    ).all()
    for row in rows:
        row.done = True


# --------------------------------------------------------------------------- #
# Locking helpers
# --------------------------------------------------------------------------- #


def _lock_instance(db: Session, instance_id: uuid.UUID) -> Instance:
    instance = db.scalar(
        select(Instance).where(Instance.id == instance_id).with_for_update()
    )
    if instance is None:
        raise DomainError("instance_not_found", f"instance {instance_id} not found", 404)
    return instance


def _lock_pending_tasks(db: Session, instance: Instance, node_id: str, generation: int) -> list[Task]:
    return list(
        db.scalars(
            select(Task)
            .where(
                Task.instance_id == instance.id,
                Task.node_id == node_id,
                Task.generation == generation,
                Task.status == "pending",
            )
            .order_by(Task.assignee)
            .with_for_update()
        ).all()
    )


# --------------------------------------------------------------------------- #
# Decisions (approve / reject) with idempotency
# --------------------------------------------------------------------------- #


def _fingerprint_matches(stored: ApprovalRequest, fingerprint: dict) -> bool:
    return (
        stored.instance_id == fingerprint["instance_id"]
        and stored.expected_version == fingerprint["expected_version"]
        and stored.actor == fingerprint["actor"]
        and stored.decision == fingerprint["decision"]
        and (stored.comment or None) == (fingerprint["comment"] or None)
    )


def decide(db: Session, body: DecisionIn) -> tuple[dict, bool]:
    """Apply an approve/reject decision.

    Returns ``(response_body, created)``; ``created=False`` means the call was
    a duplicate and the stored original result was replayed. A duplicate whose
    parameters disagree raises 409 and writes nothing.
    """
    fingerprint = {
        "instance_id": body.instance_id,
        "expected_version": body.expected_version,
        "actor": body.actor,
        "decision": body.decision,
        "comment": body.comment,
    }

    stored = db.get(ApprovalRequest, body.request_id)
    if stored is not None:
        if not _fingerprint_matches(stored, fingerprint):
            raise DomainError(
                "idempotency_conflict",
                "request_id was already used with different parameters",
                409,
            )
        return {**stored.response_snapshot, "replayed": True}, False

    from .serializers import serialize_instance

    instance = _lock_instance(db, body.instance_id)
    if instance.version_number != body.expected_version:
        raise DomainError(
            "version_conflict",
            f"instance is bound to version {instance.version_number}, "
            f"request expected {body.expected_version}",
            409,
        )
    if instance.status != "running":
        raise DomainError("instance_not_running", f"instance is {instance.status}", 409)
    if not instance.current_node_id:
        raise DomainError("no_open_node", "instance has no open approval node", 409)

    node_id = instance.current_node_id
    nodes, _ = _index_definition(instance.definition_snapshot)
    node = nodes[node_id]

    # Lock the current pending generation together.
    pending = _lock_pending_tasks(db, instance, node_id, _current_generation(db, instance, node_id))
    mine = next((t for t in pending if t.assignee == body.actor), None)
    if mine is None:
        # Distinguish "not your todo" from "your todo was already acted on".
        any_mine = db.scalar(
            select(Task.id).where(
                Task.instance_id == instance.id,
                Task.node_id == node_id,
                Task.assignee == body.actor,
            )
        )
        if any_mine is not None:
            raise DomainError("task_already_handled", "your task at this node was already handled", 409)
        raise DomainError("not_assignee", "actor is not an assignee of the current node", 403)

    result = _apply_decision(db, instance, node, mine, pending, body)

    db.flush()
    body_snapshot = {
        "request_id": body.request_id,
        "replayed": False,
        "result": result,
        "instance": serialize_instance(db, instance),
    }
    db.add(
        ApprovalRequest(
            request_id=body.request_id,
            instance_id=instance.id,
            expected_version=body.expected_version,
            actor=body.actor,
            decision=body.decision,
            comment=body.comment,
            response_snapshot=body_snapshot,
        )
    )
    db.commit()
    return body_snapshot, True


def _current_generation(db: Session, instance: Instance, node_id: str) -> int:
    return db.scalar(
        select(func.coalesce(func.max(Task.generation), 0)).where(
            Task.instance_id == instance.id, Task.node_id == node_id
        )
    )


def _apply_decision(db, instance, node, mine: Task, pending: list[Task], body: DecisionIn) -> str:
    now = utcnow()

    if body.decision == "reject":
        # Explicit rejection: the flow terminates immediately.
        mine.status = "rejected"
        mine.closed_at = now
        closed_assignees: list[str] = []
        for task in pending:
            if task.id != mine.id:
                task.status = "closed"
                task.closed_at = now
                closed_assignees.append(task.assignee)
        _add_history(db, instance, "task_decision", node_id=node["id"], actor=body.actor,
                     detail={"decision": "reject", "mode": node["mode"]}, comment=body.comment)
        if closed_assignees:
            _add_history(db, instance, "tasks_closed", node_id=node["id"],
                         detail={"assignees": closed_assignees, "reason": "rejected"})
        _cancel_escalations(db, instance, node["id"])
        instance.status = "rejected"
        instance.current_node_id = None
        instance.finished_at = now
        reason = body.comment.strip() if body.comment and body.comment.strip() else None
        instance.reject_reason = f"{body.actor} 明确拒绝" + (f": {reason}" if reason else "")
        _add_history(db, instance, "instance_rejected", node_id=node["id"], actor=body.actor,
                     detail={"reason": instance.reject_reason})
        return "rejected"

    # approve
    mine.status = "approved"
    mine.closed_at = now
    _add_history(db, instance, "task_decision", node_id=node["id"], actor=body.actor,
                 detail={"decision": "approve", "mode": node["mode"]}, comment=body.comment)

    others = [t for t in pending if t.id != mine.id]

    if node["mode"] == "any":
        # One approval is enough; the remaining open todos are closed.
        closed_assignees = []
        for task in others:
            task.status = "closed"
            task.closed_at = now
            closed_assignees.append(task.assignee)
        if closed_assignees:
            _add_history(db, instance, "tasks_closed", node_id=node["id"],
                         detail={"assignees": closed_assignees, "reason": "any_sign_approved",
                                 "approved_by": body.actor})
        _cancel_escalations(db, instance, node["id"])
        _add_history(db, instance, "approval_passed", node_id=node["id"],
                     detail={"mode": "any", "approved_by": body.actor})
        instance.current_node_id = None
        _walk_after_approval(db, instance, node)
        return "approved_advanced"

    # all sign: every assignee must approve.
    still_pending = [t for t in others if t.status == "pending"]
    if still_pending:
        _add_history(db, instance, "approval_waiting", node_id=node["id"],
                     detail={"waiting_for": [t.assignee for t in still_pending]})
        return "waiting_for_others"

    _cancel_escalations(db, instance, node["id"])
    _add_history(db, instance, "approval_passed", node_id=node["id"],
                 detail={"mode": "all"})
    instance.current_node_id = None
    _walk_after_approval(db, instance, node)
    return "approved_advanced"


def _walk_after_approval(db, instance, completed_node: dict) -> None:
    """Leave the completed approval node along its single outgoing edge."""
    outgoing = _index_definition(instance.definition_snapshot)[1]
    edge = outgoing[completed_node["id"]][0]
    instance.current_node_id = edge["target"]
    _walk(db, instance)


# --------------------------------------------------------------------------- #
# Withdraw
# --------------------------------------------------------------------------- #


def withdraw(db: Session, body: WithdrawIn) -> tuple[dict, bool]:
    fingerprint = {
        "instance_id": body.instance_id,
        "expected_version": body.expected_version,
        "actor": body.submitter,
        "decision": "withdraw",
        "comment": None,
    }
    stored = db.get(ApprovalRequest, body.request_id)
    if stored is not None:
        if (
            stored.instance_id != body.instance_id
            or stored.actor != body.submitter
            or stored.decision != "withdraw"
            or (body.expected_version is not None and stored.expected_version != body.expected_version)
        ):
            raise DomainError("idempotency_conflict",
                             "request_id was already used with different parameters", 409)
        return {**stored.response_snapshot, "replayed": True}, False

    from .serializers import serialize_instance

    instance = _lock_instance(db, body.instance_id)
    if body.expected_version is not None and instance.version_number != body.expected_version:
        raise DomainError("version_conflict",
                         f"instance is bound to version {instance.version_number}", 409)
    if instance.submitter != body.submitter:
        raise DomainError("not_submitter", "only the submitter can withdraw this instance", 403)
    if instance.status == "withdrawn":
        raise DomainError("already_withdrawn", "instance is already withdrawn", 409)
    if instance.status in ("completed", "rejected"):
        raise DomainError("instance_finished", "finished instances cannot be withdrawn", 409)

    node_id = instance.current_node_id
    pending = (
        _lock_pending_tasks(db, instance, node_id, _current_generation(db, instance, node_id))
        if node_id else []
    )
    now = utcnow()
    closed_assignees = []
    for task in pending:
        task.status = "closed"
        task.closed_at = now
        closed_assignees.append(task.assignee)
    if node_id:
        _cancel_escalations(db, instance, node_id)

    instance.status = "withdrawn"
    instance.current_node_id = None
    instance.finished_at = now
    _add_history(db, instance, "tasks_closed", node_id=node_id,
                 detail={"assignees": closed_assignees, "reason": "withdrawn"})
    _add_history(db, instance, "instance_withdrawn", actor=body.submitter)

    db.flush()
    snapshot = {
        "request_id": body.request_id,
        "replayed": False,
        "result": "withdrawn",
        "instance": serialize_instance(db, instance),
    }
    db.add(
        ApprovalRequest(
            request_id=body.request_id,
            instance_id=instance.id,
            expected_version=body.expected_version if body.expected_version is not None
            else instance.version_number,
            actor=body.submitter,
            decision="withdraw",
            comment=None,
            response_snapshot=snapshot,
        )
    )
    db.commit()
    return snapshot, True


# --------------------------------------------------------------------------- #
# Timeout escalation (durable, restart-safe, no external queue)
# --------------------------------------------------------------------------- #


def due_escalations(db: Session, limit: int = 10):
    """Due, not-yet-done jobs whose generation is still the active one.

    A job whose generation has been superseded (a newer escalation exists for
    the same instance+node) is marked done and skipped, so it never fires even
    though its due_at is in the past.
    """
    jobs = list(
        db.scalars(
            select(ScheduledEscalation)
            .where(ScheduledEscalation.done.is_(False), ScheduledEscalation.due_at <= utcnow())
            .order_by(ScheduledEscalation.due_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
    )
    live: list[ScheduledEscalation] = []
    for job in jobs:
        max_gen = db.scalar(
            select(func.coalesce(func.max(ScheduledEscalation.generation), 0)).where(
                ScheduledEscalation.instance_id == job.instance_id,
                ScheduledEscalation.node_id == job.node_id,
            )
        )
        if job.generation < max_gen:
            job.done = True
            continue
        live.append(job)
    return live


def fire_escalation(db: Session, row: ScheduledEscalation) -> str:
    """Process one due escalation inside the caller's transaction.

    Returns an outcome string: ``fired``, ``stale`` (instance moved/finished)
    or ``noop`` (the old tasks were already resolved by a racing action).
    """
    instance = _lock_instance(db, row.instance_id)
    row.done = True  # the old job can never fire twice, regardless of outcome

    if instance.status != "running" or instance.current_node_id != row.node_id:
        return "stale"

    nodes, _ = _index_definition(instance.definition_snapshot)
    node = nodes[row.node_id]
    pending = _lock_pending_tasks(db, instance, row.node_id, row.generation)
    if not pending:
        return "noop"

    now = utcnow()
    old_assignees = []
    for task in pending:
        task.status = "escalated"
        task.closed_at = now
        old_assignees.append(task.assignee)
    _add_history(db, instance, "tasks_escalated", node_id=row.node_id,
                 detail={"from_generation": row.generation, "assignees": old_assignees})

    new_generation = row.generation + 1
    _open_approval(db, instance, node, generation=new_generation,
                   assignees=node.get("escalate_to") or node["assignees"])
    return "fired"
