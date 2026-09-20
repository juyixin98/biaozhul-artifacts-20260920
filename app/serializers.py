"""ORM -> API schema serialization (definitions come from the bound snapshot)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import HistoryRecord, Instance, Task
from .schemas import HistoryOut, InstanceOut, TaskOut


def _index(instance: Instance) -> dict[str, dict]:
    return {n["id"]: n for n in instance.definition_snapshot["nodes"]}


def serialize_task(instance: Instance, task: Task) -> TaskOut:
    nodes = _index(instance)
    node = nodes.get(task.node_id, {})
    return TaskOut(
        id=task.id,
        node_id=task.node_id,
        node_name=node.get("name"),
        generation=task.generation,
        assignee=task.assignee,
        status=task.status,
        created_at=task.created_at,
        closed_at=task.closed_at,
    )


def serialize_instance(db: Session, instance: Instance) -> dict:
    nodes = _index(instance)
    current = nodes.get(instance.current_node_id, {}) if instance.current_node_id else {}

    open_tasks = list(
        db.scalars(
            select(Task)
            .where(Task.instance_id == instance.id, Task.status == "pending")
            .order_by(Task.node_id, Task.assignee)
        ).all()
    )

    # template_key is looked up via template id; keep the snapshot version here.
    from .models import Template

    template = db.get(Template, instance.template_id)
    template_key = template.key if template else None

    payload = InstanceOut(
        id=instance.id,
        template_key=template_key,
        version=instance.version_number,
        status=instance.status,
        submitter=instance.submitter,
        business_key=instance.business_key,
        current_node_id=instance.current_node_id,
        current_node_name=current.get("name"),
        reject_reason=instance.reject_reason,
        payload=instance.payload,
        created_at=instance.created_at,
        finished_at=instance.finished_at,
        open_tasks=[serialize_task(instance, t) for t in open_tasks],
    )
    return payload.model_dump(mode="json")


def serialize_history(records: list[HistoryRecord]) -> list[dict]:
    return [
        HistoryOut(
            seq=r.seq,
            action=r.action,
            node_id=r.node_id,
            actor=r.actor,
            detail=r.detail,
            comment=r.comment,
            created_at=r.created_at,
        ).model_dump(mode="json")
        for r in records
    ]
