"""流程引擎核心。

并发正确性策略（PostgreSQL）：
- 每个业务事务首先对 instance 行 ``SELECT ... FOR UPDATE``，同一实例的所有
  状态转换串行化，不同实例互不阻塞；
- 待办状态转换额外使用条件 UPDATE（``WHERE status='pending'``），双保险确保
  审批/撤回/超时升级只能产生一次有效转换；
- 状态、待办、审计历史、升级标记在**同一个数据库事务**内提交，崩溃整体回滚；
- 超时升级用 FOR UPDATE SKIP LOCKED 认领，失败保留 pending + 记录 attempts，
  下个轮询（或重启后）重试。
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import enums
from app.dsl import node_index
from app.enums import (
    EscalationStatus,
    EventType,
    InstanceStatus,
    TaskStatus,
    VersionStatus,
)
from app.errors import AuthzError, ConflictError, NotFoundError, ValidationError
from app.expressions import RestrictedSyntaxError, safe_evaluate
from app.models import (
    Escalation,
    HistoryEvent,
    IdempotencyRecord,
    Instance,
    Task,
    Template,
    TemplateVersion,
    utcnow,
)


# ---------------------------------------------------------------- 幂等支持


def _fingerprint(parts: list[Any]) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def acquire_idempotency(
    db: Session, request_id: str, scope: str, fingerprint_parts: list[Any]
) -> dict | None:
    """声明一个幂等请求。

    返回 None 表示首次请求（调用方继续业务逻辑，事务结束前需调用
    ``store_idempotency_result``）；返回 dict 表示重复请求，直接回放原结果。
    指纹不一致抛 409 —— 调用方此前未写入任何业务数据，直接回滚即可。
    """
    fp = _fingerprint(fingerprint_parts)
    try:
        db.add(
            IdempotencyRecord(
                request_id=request_id, scope=scope, fingerprint=fp
            )
        )
        db.flush()
        return None
    except IntegrityError:
        # 同一 request_id 已存在（并发请求或重放）。回滚到事务初始状态后读取。
        db.rollback()
        existing = db.get(IdempotencyRecord, request_id)
        if existing is None:
            # 极端情况下原事务回滚了：按首次请求重新声明。
            return acquire_idempotency(db, request_id, scope, fingerprint_parts)
        if existing.fingerprint != fp:
            raise ConflictError(
                "request_id 已用于不同的请求（幂等指纹冲突），本次请求被拒绝且未写入任何数据"
            )
        if existing.response_body is None:
            # 原请求仍在进行中（并发重复），要求客户端稍后重试。
            raise ConflictError("相同 request_id 的请求正在处理中，请稍后重试")
        return existing.response_body


def store_idempotency_result(db: Session, request_id: str, result: dict) -> None:
    record = db.get(IdempotencyRecord, request_id)
    if record is not None:
        record.response_body = result
        record.status_code = 200
        db.flush()


# ---------------------------------------------------------------- 模板与版本


def create_template(db: Session, key: str, name: str, definition: dict) -> Template:
    if db.scalar(select(Template).where(Template.key == key)):
        raise ConflictError(f"模板 {key} 已存在")
    template = Template(key=key, name=name, current_version_id=None)
    db.add(template)
    db.flush()
    add_version(db, template, definition)
    db.commit()
    return template


def add_version(db: Session, template: Template, definition: dict) -> TemplateVersion:
    # 延迟导入避免循环依赖
    from app.dsl import validate_definition

    validate_definition(definition)
    last = db.scalar(
        select(TemplateVersion)
        .where(TemplateVersion.template_id == template.id)
        .order_by(TemplateVersion.version.desc())
    )
    version = TemplateVersion(
        template_id=template.id,
        version=(last.version + 1) if last else 1,
        status=VersionStatus.DRAFT,
        definition=definition,
    )
    db.add(version)
    db.flush()
    db.commit()
    return version


def publish_version(db: Session, key: str, version_number: int) -> TemplateVersion:
    template = _get_template(db, key)
    version = _get_version(db, template.id, version_number)
    if version.status == VersionStatus.PUBLISHED:
        raise ConflictError(f"版本 {version_number} 已发布，发布后不可变")
    # 发布前再次校验（草稿期间定义不会被修改，这里是防御性的最终门禁）。
    from app.dsl import validate_definition

    validate_definition(version.definition)
    version.status = VersionStatus.PUBLISHED
    version.published_at = utcnow()
    # 指针切换：只影响之后创建的实例。
    template.current_version_id = version.id
    db.commit()
    return version


def rollback_template(db: Session, key: str, version_number: int) -> TemplateVersion:
    """将当前版本指针切回某个已发布版本。不复制、不修改任何版本定义。"""
    template = _get_template(db, key)
    target = _get_version(db, template.id, version_number)
    if target.status != VersionStatus.PUBLISHED:
        raise ConflictError(f"版本 {version_number} 未发布，不能回滚到该版本")
    if template.current_version_id == target.id:
        raise ConflictError(f"当前已经是版本 {version_number}")
    template.current_version_id = target.id
    db.commit()
    return target


def _get_template(db: Session, key: str) -> Template:
    template = db.scalar(select(Template).where(Template.key == key))
    if template is None:
        raise NotFoundError(f"模板 {key} 不存在")
    return template


def _get_version(db: Session, template_id: int, version_number: int) -> TemplateVersion:
    version = db.scalar(
        select(TemplateVersion).where(
            TemplateVersion.template_id == template_id,
            TemplateVersion.version == version_number,
        )
    )
    if version is None:
        raise NotFoundError(f"版本 {version_number} 不存在")
    return version


# ---------------------------------------------------------------- 实例启动


def start_instance(
    db: Session,
    template_key: str,
    submitter: str,
    context: dict,
    request_id: str,
) -> tuple[Instance, dict | None]:
    """创建实例并绑定模板的**当前发布版本**。返回 (实例, 重放结果或 None)。"""
    replay = acquire_idempotency(
        db,
        request_id,
        scope="start",
        fingerprint_parts=[template_key, submitter, context],
    )
    if replay is not None:
        instance = db.get(Instance, replay["instance_id"])
        return instance, replay

    template = _get_template(db, template_key)
    if template.current_version_id is None:
        raise ValidationError(f"模板 {template_key} 还没有已发布版本")
    version = db.get(TemplateVersion, template.current_version_id)
    if version is None or version.status != VersionStatus.PUBLISHED:
        raise ConflictError(f"模板 {template_key} 当前指向的版本不可用")

    definition = version.definition
    start_node_id = definition["start_node"]

    instance = Instance(
        template_id=template.id,
        template_version_id=version.id,  # 绑定后永不更新
        version_number=version.version,
        status=InstanceStatus.RUNNING,
        submitter=submitter,
        context=context or {},
        current_node_id=start_node_id,
        start_request_id=request_id,
    )
    db.add(instance)
    db.flush()

    _history(db, instance, start_node_id, EventType.START, actor=submitter)
    # start 节点是自动节点，立即激活其下游。
    nodes = node_index(definition)
    _advance(db, instance, definition, nodes, start_node_id)

    result = _instance_payload(db, instance)
    store_idempotency_result(db, request_id, result)
    db.commit()
    return instance, None


# ---------------------------------------------------------------- 节点推进


def _advance(
    db: Session,
    instance: Instance,
    definition: dict,
    nodes: dict[str, dict],
    from_node_id: str,
) -> None:
    """沿自动路径激活节点：条件节点当场求值，直到遇到审批节点或结束节点。"""
    current = nodes[from_node_id]
    # 取 from 节点的默认出边（start/approval 用 next；条件节点由分支函数处理）。
    next_id = current.get("next")
    _activate(db, instance, definition, nodes, next_id)


def _activate(
    db: Session,
    instance: Instance,
    definition: dict,
    nodes: dict[str, dict],
    node_id: str,
) -> None:
    node = nodes[node_id]
    ntype = node["type"]
    instance.current_node_id = node_id

    if ntype == "approval":
        now = utcnow()
        for assignee in node["assignees"]:
            db.add(
                Task(
                    instance_id=instance.id,
                    node_id=node_id,
                    assignee=assignee,
                    status=TaskStatus.PENDING,
                )
            )
        timeout = node.get("timeout")
        if timeout:
            db.add(
                Escalation(
                    instance_id=instance.id,
                    node_id=node_id,
                    status=EscalationStatus.PENDING,
                    due_at=now + timedelta(seconds=timeout["seconds"]),
                    targets=timeout["targets"],
                )
            )
        db.flush()
        return

    if ntype == "condition":
        chosen = node["default"]
        branch_label = "default"
        for idx, branch in enumerate(node["branches"]):
            try:
                hit = safe_evaluate(branch["when"], instance.context)
            except RestrictedSyntaxError as exc:  # 发布前已校验，运行时仅防御类型问题
                raise ConflictError(f"条件执行失败: {exc}") from exc
            if hit:
                chosen = branch["next"]
                branch_label = f"branches[{idx}]"
                break
        _history(
            db,
            instance,
            node_id,
            EventType.BRANCH,
            detail={"chosen": chosen, "matched": branch_label},
        )
        _activate(db, instance, definition, nodes, chosen)
        return

    if ntype == "end":
        instance.status = InstanceStatus.COMPLETED
        instance.closed_at = utcnow()
        _history(db, instance, node_id, EventType.COMPLETE)
        db.flush()
        return

    raise ConflictError(f"非法的节点类型: {ntype}")  # 发布前校验保证不可达


# ---------------------------------------------------------------- 审批决策


def decide_task(
    db: Session,
    instance_id: str,
    task_id: int,
    *,
    request_id: str,
    expected_version: int,
    actor: str,
    decision: str,
    comment: str | None = None,
) -> tuple[Instance, dict | None]:
    fingerprint = [instance_id, task_id, expected_version, actor, decision, comment]
    replay = acquire_idempotency(db, request_id, scope="task_action", fingerprint_parts=fingerprint)
    if replay is not None:
        return db.get(Instance, instance_id), replay

    instance = _lock_instance(db, instance_id)

    # 预期版本：实例始终按绑定版本执行，版本不符直接冲突，不写历史。
    if instance.version_number != expected_version:
        raise ConflictError(
            f"版本冲突：请求预期版本 {expected_version}，实例绑定版本 {instance.version_number}，"
            "本次操作未写入任何数据"
        )
    if instance.status != InstanceStatus.RUNNING:
        raise ConflictError(f"流程已结束（{instance.status}），不能继续审批")

    task = db.get(Task, task_id)
    # 关键：待办必须属于路径中的实例 —— 不能靠替换实例 ID 操作他人待办。
    if task is None or task.instance_id != instance.id:
        raise NotFoundError("待办不存在")
    if task.assignee != actor:
        raise AuthzError("只有该待办的指定审批人可以操作")
    if task.status != TaskStatus.PENDING:
        raise ConflictError(f"待办已处理（{task.status}），重复/并发操作只有一次生效")

    if decision not in ("approve", "reject"):
        raise ValidationError("decision 必须是 approve 或 reject")
    # 归一化为任务存储状态 approve -> approved
    decision = TaskStatus.APPROVED if decision == "approve" else TaskStatus.REJECTED

    now = utcnow()
    # 条件 UPDATE：仅当仍为 pending 时生效，并发下只有一行 rowcount=1。
    rowcount = db.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == TaskStatus.PENDING)
        .values(
            status=decision,
            decision=decision,
            handled_by=actor,
            comment=comment,
            handled_at=now,
        )
    ).rowcount
    if rowcount != 1:
        raise ConflictError("待办已被并发处理，本次操作未生效")

    node = node_index(_bound_definition(db, instance))[task.node_id]
    mode = node["mode"]

    _history(
        db,
        instance,
        task.node_id,
        EventType.TASK_APPROVE if decision == TaskStatus.APPROVED else EventType.TASK_REJECT,
        actor=actor,
        detail={"task_id": task.id, "mode": mode, "comment": comment},
    )

    if decision == TaskStatus.REJECTED:
        _handle_rejection(db, instance, node, task, actor, comment)
    else:
        _handle_approval(db, instance, node, task, mode)

    result = _instance_payload(db, instance)
    store_idempotency_result(db, request_id, result)
    db.commit()
    return instance, None


def _handle_rejection(
    db: Session,
    instance: Instance,
    node: dict,
    rejected_task: Task,
    actor: str,
    comment: str | None,
) -> None:
    """明确拒绝规则：

    - 全签（all）：需要全部通过，任一人拒绝即节点不通过 → 整个流程拒绝结束，
      其余待办立即关闭；
    - 任签（any）：一人通过即推进；若某人拒绝而仍有其他待办未处理，流程继续
      等待；当所有审批人都未通过（全部拒绝）时，节点不通过 → 流程拒绝结束。
    """
    pending = _pending_tasks(db, instance.id, node["id"])

    if node["mode"] == "all" or not pending:
        # all：一票否决。any：所有人都拒绝了，不再可能通过。
        if pending:
            _cancel_pending(db, instance, node["id"], pending, reason="node_rejected")
        _cancel_escalations(db, instance.id, node["id"])
        reason = comment or f"审批人 {actor} 拒绝"
        instance.status = InstanceStatus.REJECTED
        instance.reject_reason = reason
        instance.closed_at = utcnow()
        _history(
            db,
            instance,
            node["id"],
            EventType.REJECT,
            actor=actor,
            detail={"reason": reason, "mode": node["mode"]},
        )
    # any 模式且仍有 pending：仅记录该审批人的拒绝，等待其他人。


def _handle_approval(
    db: Session, instance: Instance, node: dict, approved_task: Task, mode: str
) -> None:
    pending = _pending_tasks(db, instance.id, node["id"])

    passed = mode == "any" or not pending
    if not passed:
        # 全签：等待其余审批人。
        return

    if pending:
        # 任签通过：关闭其余待办。
        _cancel_pending(db, instance, node["id"], pending, reason="sibling_approved")
        _history(
            db,
            instance,
            node["id"],
            EventType.TASKS_CANCELED,
            detail={"reason": "any_mode_first_approval"},
        )

    _cancel_escalations(db, instance.id, node["id"])
    _history(
        db,
        instance,
        node["id"],
        EventType.NODE_APPROVE,
        detail={"mode": mode},
    )
    definition = _bound_definition(db, instance)
    nodes = node_index(definition)
    _advance(db, instance, definition, nodes, node["id"])


def _pending_tasks(db: Session, instance_id: str, node_id: str) -> list[Task]:
    return list(
        db.scalars(
            select(Task).where(
                Task.instance_id == instance_id,
                Task.node_id == node_id,
                Task.status == TaskStatus.PENDING,
            )
        )
    )


def _cancel_pending(
    db: Session, instance: Instance, node_id: str, tasks: list[Task], *, reason: str
) -> int:
    if not tasks:
        return 0
    ids = [t.id for t in tasks]
    rowcount = db.execute(
        update(Task)
        .where(Task.id.in_(ids), Task.status == TaskStatus.PENDING)
        .values(status=TaskStatus.CANCELED, handled_at=utcnow())
    ).rowcount
    return rowcount or 0


# ---------------------------------------------------------------- 撤回


def withdraw(
    db: Session, instance_id: str, *, request_id: str, actor: str
) -> tuple[Instance, dict | None]:
    replay = acquire_idempotency(
        db, request_id, scope="withdraw", fingerprint_parts=[instance_id, actor]
    )
    if replay is not None:
        return db.get(Instance, instance_id), replay

    instance = _lock_instance(db, instance_id)
    if instance.submitter != actor:
        raise AuthzError("只有提交人可以撤回")
    if instance.status in enums.InstanceStatus.CLOSED:
        raise ConflictError(f"流程已结束（{instance.status}），不能撤回")

    pending = db.scalars(
        select(Task).where(
            Task.instance_id == instance.id, Task.status == TaskStatus.PENDING
        )
    ).all()
    _cancel_pending(
        db, instance, instance.current_node_id, list(pending), reason="withdrawn"
    )
    _cancel_escalations(db, instance.id, instance.current_node_id)

    instance.status = InstanceStatus.WITHDRAWN
    instance.closed_at = utcnow()
    _history(db, instance, instance.current_node_id, EventType.WITHDRAW, actor=actor)

    result = _instance_payload(db, instance)
    store_idempotency_result(db, request_id, result)
    db.commit()
    return instance, None


# ---------------------------------------------------------------- 超时升级


def process_due_escalations(db: Session, *, limit: int) -> int:
    """认领并处理到期升级。由后台轮询循环调用，返回处理条数。

    - FOR UPDATE SKIP LOCKED：多实例部署/多线程互不重复认领；
    - 每条升级独立事务，失败仅记录错误并保留 pending，下轮重试，
      服务重启后 due_at 已过的记录会立即被重新处理。
    """
    now = utcnow()
    due = db.scalars(
        select(Escalation)
        .where(Escalation.status == EscalationStatus.PENDING, Escalation.due_at <= now)
        .order_by(Escalation.due_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()

    processed = 0
    for esc in due:
        try:
            _apply_escalation(db, esc)
            db.commit()
            processed += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其他升级
            db.rollback()
            _record_escalation_failure(esc.id, exc)
    return processed


def _apply_escalation(db: Session, esc: Escalation) -> None:
    instance = db.scalar(
        select(Instance)
        .where(Instance.id == esc.instance_id)
        .with_for_update()
    )
    if instance is None:
        esc.status = EscalationStatus.CANCELED
        esc.processed_at = utcnow()
        return

    # 节点已经推进 / 流程结束 / 已撤回：升级作废，且不能产生第二次状态转换。
    if (
        instance.status != InstanceStatus.RUNNING
        or instance.current_node_id != esc.node_id
    ):
        esc.status = EscalationStatus.CANCELED
        esc.processed_at = utcnow()
        return

    pending = _pending_tasks(db, instance.id, esc.node_id)
    if not pending:
        # 待办刚被审批/撤回处理但行尚未取消（理论上同一把实例锁使其不可见）。
        esc.status = EscalationStatus.CANCELED
        esc.processed_at = utcnow()
        return

    _cancel_pending(
        db, instance, esc.node_id, pending, reason="escalated_timeout"
    )
    for target in esc.targets:
        db.add(
            Task(
                instance_id=instance.id,
                node_id=esc.node_id,
                assignee=target,
                status=TaskStatus.PENDING,
            )
        )
    _history(
        db,
        instance,
        esc.node_id,
        EventType.ESCALATE,
        detail={"from": [t.assignee for t in pending], "to": list(esc.targets)},
    )
    esc.status = EscalationStatus.DONE
    esc.processed_at = utcnow()
    db.flush()


def _record_escalation_failure(escalation_id: int, exc: Exception) -> None:
    """独立事务记录失败，保证下轮可重试。"""
    from app.db import SessionLocal

    with SessionLocal() as fail_db:
        record = fail_db.get(Escalation, escalation_id)
        if record is not None and record.status == EscalationStatus.PENDING:
            record.attempts += 1
            record.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            fail_db.commit()


# ---------------------------------------------------------------- 查询


def get_instance(db: Session, instance_id: str) -> Instance:
    instance = db.get(Instance, instance_id)
    if instance is None:
        raise NotFoundError("流程实例不存在")
    return instance


def get_history(db: Session, instance_id: str) -> list[HistoryEvent]:
    get_instance(db, instance_id)
    return list(
        db.scalars(
            select(HistoryEvent)
            .where(HistoryEvent.instance_id == instance_id)
            .order_by(HistoryEvent.id)
        )
    )


# ---------------------------------------------------------------- 内部工具


def _lock_instance(db: Session, instance_id: str) -> Instance:
    instance = db.scalar(
        select(Instance).where(Instance.id == instance_id).with_for_update()
    )
    if instance is None:
        raise NotFoundError("流程实例不存在")
    return instance


def _bound_definition(db: Session, instance: Instance) -> dict:
    # 永远从实例绑定的版本读取定义 —— 版本隔离。
    version = db.get(TemplateVersion, instance.template_version_id)
    return version.definition


def _cancel_escalations(db: Session, instance_id: str, node_id: str | None) -> None:
    if node_id is None:
        return
    db.execute(
        update(Escalation)
        .where(
            Escalation.instance_id == instance_id,
            Escalation.node_id == node_id,
            Escalation.status == EscalationStatus.PENDING,
        )
        .values(status=EscalationStatus.CANCELED, processed_at=utcnow())
    )


def _history(
    db: Session,
    instance: Instance,
    node_id: str | None,
    event_type: str,
    *,
    actor: str | None = None,
    detail: dict | None = None,
) -> None:
    db.add(
        HistoryEvent(
            instance_id=instance.id,
            node_id=node_id,
            event_type=event_type,
            actor=actor,
            detail=detail or {},
        )
    )
    db.flush()


def _instance_payload(db: Session, instance: Instance) -> dict:
    tasks = list(
        db.scalars(
            select(Task)
            .where(Task.instance_id == instance.id)
            .order_by(Task.id)
        )
    )
    return {
        "instance_id": instance.id,
        "status": instance.status,
        "template_version": instance.version_number,
        "submitter": instance.submitter,
        "current_node_id": instance.current_node_id,
        "reject_reason": instance.reject_reason,
        "pending_tasks": [
            {"task_id": t.id, "node_id": t.node_id, "assignee": t.assignee}
            for t in tasks
            if t.status == TaskStatus.PENDING
        ],
    }
