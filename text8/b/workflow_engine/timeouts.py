"""超时升级扫描。

- 行锁（FOR UPDATE SKIP LOCKED）保证并发审批 / 撤回 / 升级只产生一次有效转换；
- 每个实例一个 SAVEPOINT，单条升级失败只回滚该实例，下轮扫描继续重试；
- 服务重启后 pending 且 deadline 已过的 activity 会被再次捞起，天然支持恢复。
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from workflow_engine.constants import (
    AuditActorType,
    EventType,
    InstanceStatus,
    TaskStatus,
    TimeoutAction,
)
from workflow_engine.engine import ACTIVITY_COMPLETED, ACTIVITY_TIMEOUT_RESOLVED, FlowEngine, utcnow
from workflow_engine.models import Instance, NodeActivity, Task


def _due_instance_ids(session: Session, now: datetime, limit: int) -> list[int]:
    """先锁实例（按 id 排序），与审批路径的加锁顺序一致，避免死锁。

    审批事务先锁 instance 再锁 activity/task；这里同样先 instance 后 activity。
    """
    rows = session.execute(
        select(Instance.id)
        .join(NodeActivity, NodeActivity.instance_id == Instance.id)
        .where(
            Instance.status == InstanceStatus.RUNNING,
            NodeActivity.status == "pending",
            NodeActivity.deadline.is_not(None),
            NodeActivity.deadline <= now,
        )
        .order_by(Instance.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    return [r[0] for r in rows]


def _process_instance(session: Session, instance_id: int, now: datetime, definition: dict) -> list[str]:
    """处理单个实例上所有到期 activity；返回处理动作描述。"""
    engine = FlowEngine(session)
    nodes = engine._node_map(definition)
    actions: list[str] = []

    activities = list(
        session.scalars(
            select(NodeActivity)
            .where(
                NodeActivity.instance_id == instance_id,
                NodeActivity.status == "pending",
                NodeActivity.deadline.is_not(None),
                NodeActivity.deadline <= now,
            )
            .order_by(NodeActivity.id)
            .with_for_update()
        )
    )

    for activity in activities:
        node = nodes.get(activity.node_id)
        if node is None or node.get("timeout_action") is None:
            # 定义已冻结且来自发布版本，理论不会发生；标记避免反复扫描
            activity.deadline = None
            continue

        # 再检查一次该 activity 仍有 pending 待办（可能已被并发事务处理）
        pending = list(
            session.scalars(
                select(Task)
                .where(Task.activity_id == activity.id, Task.status == TaskStatus.PENDING)
                .with_for_update()
            )
        )
        if not pending:
            activity.status = ACTIVITY_COMPLETED
            continue

        instance = session.get(Instance, instance_id)
        action = node["timeout_action"]

        if action["type"] == TimeoutAction.ESCALATE:
            if activity.escalated:
                # 已升级过一次：升级待办也超时了，不再继续升级，留待人工处理；
                # 清除 deadline 防止重复扫描
                activity.deadline = None
                actions.append(f"{activity.node_id}:escalation_waiting")
                continue
            # 关闭旧待办
            for t in pending:
                t.status = TaskStatus.TIMEOUT_CLOSED
                t.decided_at = now
            engine._audit(
                instance,
                EventType.ESCALATE,
                node_id=node["id"],
                actor=None,
                actor_type=AuditActorType.SYSTEM,
                detail={"from": [t.assignee for t in pending], "to": list(action["to"])},
            )
            # 建立升级待办；升级待办不再设置 deadline（只升级一次）
            for target in action["to"]:
                session.add(
                    Task(
                        instance_id=instance.id,
                        activity_id=activity.id,
                        node_id=node["id"],
                        assignee=target,
                        status=TaskStatus.PENDING,
                    )
                )
            activity.escalated = True
            activity.deadline = None
            actions.append(f"{activity.node_id}:escalated")

        elif action["type"] == TimeoutAction.AUTO_APPROVE:
            for t in pending:
                t.status = TaskStatus.APPROVED
                t.decided_at = now
                t.comment = "超时自动通过"
            activity.status = ACTIVITY_TIMEOUT_RESOLVED
            engine._audit(
                instance,
                EventType.AUTO_APPROVE,
                node_id=node["id"],
                actor=None,
                actor_type=AuditActorType.SYSTEM,
                detail={"timeout": True},
            )
            engine._advance(instance, definition, nodes, node["next"], actor=None)
            actions.append(f"{activity.node_id}:auto_approved")
            # 自动通过后可能已进入后续节点甚至结束，后续 activity 属于新一轮
            break

        else:  # AUTO_REJECT
            for t in pending:
                t.status = TaskStatus.REJECTED
                t.decided_at = now
                t.comment = action.get("reason") or "超时自动拒绝"
            activity.status = ACTIVITY_TIMEOUT_RESOLVED
            reason = action.get("reason") or "超时自动拒绝"
            engine._reject_path(
                instance,
                definition,
                nodes,
                node,
                reason,
                actor=None,
                actor_type=AuditActorType.SYSTEM,
                close_event=EventType.AUTO_REJECT,
            )
            actions.append(f"{activity.node_id}:auto_rejected")
            break

    return actions


def run_sweep(
    session: Session,
    *,
    get_definition,
    now: datetime | None = None,
    batch_size: int = 50,
) -> dict:
    """扫描并处理到期升级。

    - 第一个事务只负责用 ``FOR UPDATE SKIP LOCKED`` 选出到期实例；
    - 每个实例独立事务处理，任何异常整笔回滚，下一轮扫描自动重试
      （因此任务失败可重试、服务重启后继续处理未完成升级）；
    - 处理事务重新锁定实例行（与审批事务的加锁顺序 instance→activity→task 一致），
      与并发审批/撤回竞争时只有一方能推进。

    ``get_definition(session, instance) -> dict`` 由调用方提供（从版本表加载不可变定义）。
    """
    now = now or datetime.now(timezone.utc)
    summary: dict[str, list[str]] = {"processed": [], "failed": []}

    with session.begin():
        instance_ids = _due_instance_ids(session, now, batch_size)

    for instance_id in instance_ids:
        try:
            with session.begin():
                instance = session.get(Instance, instance_id, with_for_update=True)
                if instance is None or instance.status != InstanceStatus.RUNNING:
                    continue
                definition = get_definition(session, instance)
                actions = _process_instance(session, instance_id, now, definition)
                summary["processed"].extend(actions)
        except Exception:
            # 单实例失败不影响其他实例；未提交即未生效，下轮重试
            session.rollback()
            summary["failed"].append(str(instance_id))

    return summary
