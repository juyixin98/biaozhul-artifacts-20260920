"""ORM -> API schema serialization helpers."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import TASK_PENDING
from app.models import AuditEvent, Instance, Task, TemplateVersion
from app.schemas import (
    AuditOut,
    InstanceDetailOut,
    InstanceOut,
    TaskOut,
    TemplateVersionOut,
)


def task_out(task: Task) -> TaskOut:
    return TaskOut.model_validate(task, from_attributes=True)


def template_out(tpl: TemplateVersion) -> TemplateVersionOut:
    return TemplateVersionOut.model_validate(tpl, from_attributes=True)


def instance_out(instance: Instance, *, include_history: bool = False,
                 history: list[AuditEvent] | None = None):
    pending = [
        task_out(t)
        for t in sorted(instance.tasks, key=lambda t: t.id)
        if t.node_id == instance.current_node_id and t.status == TASK_PENDING
    ]
    common = dict(
        id=instance.id,
        template_code=instance.template_code,
        template_version=instance.template_version,
        business_key=instance.business_key,
        variables=instance.variables,
        status=instance.status,
        current_node_id=instance.current_node_id,
        submitter=instance.submitter,
        reject_reason=instance.reject_reason,
        created_at=instance.created_at,
        updated_at=instance.updated_at,
        pending_tasks=pending,
    )
    if include_history:
        return InstanceDetailOut(
            **common,
            history=[
                AuditOut.model_validate(a, from_attributes=True)
                for a in (history or [])
            ],
        )
    return InstanceOut(**common)


async def load_history(session: AsyncSession, instance_id: int) -> list[AuditEvent]:
    return list(
        await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.instance_id == instance_id)
            .order_by(AuditEvent.id)
        )
    )
