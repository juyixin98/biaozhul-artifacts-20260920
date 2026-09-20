"""实例服务：发起、视图组装。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from workflow_engine.constants import InstanceStatus, TaskStatus
from workflow_engine.engine import FlowEngine
from workflow_engine.errors import ConflictError, NotFoundError
from workflow_engine.models import AuditLog, Instance, Task, TemplateVersion
from workflow_engine.services.templates import resolve_version_for_start


def start_instance(
    session: Session,
    *,
    template_key: str,
    version: int | None,
    title: str,
    submitter: str,
    business_key: str | None,
    context: dict,
) -> Instance:
    template, tv = resolve_version_for_start(session, template_key, version)
    instance = Instance(
        template_id=template.id,
        version_id=tv.id,
        version_number=tv.version,
        business_key=business_key,
        title=title,
        submitter=submitter,
        status=InstanceStatus.RUNNING,
        context=context or {},
    )
    session.add(instance)
    session.flush()
    FlowEngine(session).start_at_begin(instance, tv.definition)
    return instance


def get_instance(session: Session, instance_id: int) -> Instance:
    instance = session.get(Instance, instance_id)
    if instance is None:
        raise NotFoundError(f"流程实例不存在: {instance_id}", code="instance_not_found")
    return instance


def check_expected_version(instance: Instance, expected_version: int) -> None:
    if expected_version != instance.version_number:
        raise ConflictError(
            f"expected_version={expected_version} 与实例实际版本 v{instance.version_number} 不一致",
            code="version_conflict",
        )


def build_detail(session: Session, instance: Instance) -> dict:
    tasks = list(
        session.scalars(
            select(Task)
            .where(Task.instance_id == instance.id, Task.status == TaskStatus.PENDING)
            .order_by(Task.id)
        )
    )
    history = list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.instance_id == instance.id)
            .order_by(AuditLog.id)
        )
    )
    return {
        "id": instance.id,
        "template_id": instance.template_id,
        "version_id": instance.version_id,
        "version_number": instance.version_number,
        "business_key": instance.business_key,
        "title": instance.title,
        "submitter": instance.submitter,
        "status": instance.status,
        "current_node_id": instance.current_node_id,
        "context": instance.context,
        "reject_reason": instance.reject_reason,
        "completed_at": instance.completed_at,
        "created_at": instance.created_at,
        "pending_tasks": [
            {
                "id": t.id,
                "node_id": t.node_id,
                "assignee": t.assignee,
                "status": t.status,
                "decided_at": t.decided_at,
                "comment": t.comment,
            }
            for t in tasks
        ],
        "history": [
            {
                "id": h.id,
                "event_type": h.event_type,
                "node_id": h.node_id,
                "actor": h.actor,
                "actor_type": h.actor_type,
                "detail": h.detail,
                "request_id": h.request_id,
                "created_at": h.created_at,
            }
            for h in history
        ],
    }
