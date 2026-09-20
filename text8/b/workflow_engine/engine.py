"""流程运行时状态机。

所有写操作都在调用方开启的事务内执行；依赖 PostgreSQL 行锁保证
并发审批 / 撤回 / 超时升级只产生一次有效状态转换，状态、待办、审计同事务提交。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from workflow_engine.constants import (
    ApprovalMode,
    AuditActorType,
    EventType,
    InstanceStatus,
    NodeType,
    TaskStatus,
    TimeoutAction,
)
from workflow_engine.errors import ConflictError, NotFoundError
from workflow_engine.expressions import ExpressionError, safe_eval
from workflow_engine.models import AuditLog, Instance, NodeActivity, Task

ACTIVITY_PENDING = "pending"
ACTIVITY_COMPLETED = "completed"
ACTIVITY_REJECTED = "rejected"
ACTIVITY_WITHDRAWN = "withdrawn"
ACTIVITY_TIMEOUT_RESOLVED = "timeout_resolved"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FlowEngine:
    def __init__(self, session: Session):
        self.s = session

    # ------------------------------------------------------------ 工具

    def _audit(
        self,
        instance: Instance,
        event_type: str,
        *,
        node_id: str | None = None,
        actor: str | None,
        actor_type: str,
        detail: dict | None = None,
        request_id: str | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            instance_id=instance.id,
            event_type=event_type,
            node_id=node_id,
            actor=actor,
            actor_type=actor_type,
            detail=detail or {},
            request_id=request_id,
        )
        self.s.add(entry)
        return entry

    def _lock_instance(self, instance_id: int) -> Instance:
        instance = self.s.get(Instance, instance_id, with_for_update=True)
        if instance is None:
            raise NotFoundError(f"流程实例不存在: {instance_id}", code="instance_not_found")
        return instance

    def _node_map(self, definition: dict) -> dict[str, dict]:
        return {n["id"]: n for n in definition["nodes"]}

    def _pending_activities(self, instance: Instance, *, lock: bool = False):
        stmt = select(NodeActivity).where(
            NodeActivity.instance_id == instance.id,
            NodeActivity.status == ACTIVITY_PENDING,
        )
        if lock:
            stmt = stmt.with_for_update()
        return list(self.s.scalars(stmt))

    def _close_pending_tasks(
        self,
        instance: Instance,
        activity: NodeActivity,
        status: str,
        *,
        actor: str | None,
        actor_type: str,
        event_type: str,
        detail: dict,
        request_id: str | None = None,
    ) -> int:
        tasks = list(
            self.s.scalars(
                select(Task)
                .where(Task.activity_id == activity.id, Task.status == TaskStatus.PENDING)
                .with_for_update()
            )
        )
        for t in tasks:
            t.status = status
            t.decided_at = utcnow()
        self._audit(
            instance,
            event_type,
            node_id=activity.node_id,
            actor=actor,
            actor_type=actor_type,
            detail={"closed": [t.assignee for t in tasks], **detail},
            request_id=request_id,
        )
        return len(tasks)

    # ------------------------------------------------------------ 启动

    def start_at_begin(self, instance: Instance, definition: dict) -> None:
        """实例创建后从 start 节点开始推进（条件节点会被同步穿过）。"""
        nodes = self._node_map(definition)
        start = next(n for n in definition["nodes"] if n["type"] == NodeType.START)
        self._audit(
            instance,
            EventType.START,
            node_id=start["id"],
            actor=instance.submitter,
            actor_type=AuditActorType.SUBMITTER,
            detail={"title": instance.title},
        )
        self._advance(instance, definition, nodes, start["next"], actor=instance.submitter)

    # ------------------------------------------------------------ 推进

    def _advance(
        self,
        instance: Instance,
        definition: dict,
        nodes: dict[str, dict],
        target_id: str,
        *,
        actor: str | None,
    ) -> None:
        node = nodes.get(target_id)
        if node is None:
            # 已通过发布期校验；防御性处理
            raise ConflictError(f"目标节点不存在: {target_id}", code="missing_reference")

        if node["type"] == NodeType.END:
            self._enter_end(instance, node)
        elif node["type"] == NodeType.APPROVAL:
            self._enter_approval(instance, node)
        elif node["type"] == NodeType.CONDITION:
            self._enter_condition(instance, definition, nodes, node, actor=actor)
        else:
            raise ConflictError(f"不能直接进入 {node['type']} 节点: {node['id']}")

    def _enter_end(self, instance: Instance, node: dict) -> None:
        outcome = node.get("outcome", "approved")
        instance.current_node_id = node["id"]
        instance.status = InstanceStatus.APPROVED if outcome == "approved" else InstanceStatus.REJECTED
        instance.completed_at = utcnow()
        self._audit(
            instance,
            EventType.COMPLETE,
            node_id=node["id"],
            actor=None,
            actor_type=AuditActorType.SYSTEM,
            detail={"outcome": outcome},
        )

    def _enter_approval(self, instance: Instance, node: dict) -> None:
        deadline = None
        if node.get("timeout_seconds"):
            deadline = utcnow() + timedelta(seconds=node["timeout_seconds"])
        activity = NodeActivity(
            instance_id=instance.id,
            node_id=node["id"],
            status=ACTIVITY_PENDING,
            deadline=deadline,
        )
        self.s.add(activity)
        self.s.flush()  # 拿到 activity.id
        for approver in node["approvers"]:
            self.s.add(
                Task(
                    instance_id=instance.id,
                    activity_id=activity.id,
                    node_id=node["id"],
                    assignee=approver,
                    status=TaskStatus.PENDING,
                )
            )
        instance.current_node_id = node["id"]
        self._audit(
            instance,
            EventType.ENTER_APPROVAL,
            node_id=node["id"],
            actor=None,
            actor_type=AuditActorType.SYSTEM,
            detail={"mode": node["mode"], "approvers": list(node["approvers"]), "deadline": deadline.isoformat() if deadline else None},
        )

    def _enter_condition(
        self,
        instance: Instance,
        definition: dict,
        nodes: dict[str, dict],
        node: dict,
        *,
        actor: str | None,
    ) -> None:
        chosen = None
        eval_errors: list[str] = []
        for branch in node["branches"]:
            try:
                if safe_eval(branch["when"], instance.context or {}):
                    chosen = branch
                    break
            except ExpressionError as exc:
                eval_errors.append(f"{branch['when']!r}: {exc}")

        if chosen is not None:
            self._audit(
                instance,
                EventType.CONDITION_MATCH,
                node_id=node["id"],
                actor=None,
                actor_type=AuditActorType.SYSTEM,
                detail={"when": chosen["when"], "next": chosen["next"]},
            )
            self._advance(instance, definition, nodes, chosen["next"], actor=actor)
            return

        default_id = node.get("default")
        if not default_id:
            raise ConflictError(
                f"条件节点 {node['id']} 没有匹配分支且无 default"
                + (f"；求值错误: {'; '.join(eval_errors)}" if eval_errors else ""),
                code="condition_unmatched",
            )
        self._audit(
            instance,
            EventType.CONDITION_DEFAULT,
            node_id=node["id"],
            actor=None,
            actor_type=AuditActorType.SYSTEM,
            detail={"next": default_id},
        )
        self._advance(instance, definition, nodes, default_id, actor=actor)

    # ------------------------------------------------------------ 拒绝路径

    def _reject_path(
        self,
        instance: Instance,
        definition: dict,
        nodes: dict[str, dict],
        node: dict,
        reason: str,
        *,
        actor: str | None,
        actor_type: str,
        request_id: str | None = None,
        close_event: str = EventType.CLOSE_TASKS,
    ) -> None:
        """明确拒绝：关闭该节点其余待办；on_reject 指向 end(rejected) 或其他节点。"""
        activity = self._current_pending_activity(instance, node["id"])
        other_pending = list(
            self.s.scalars(
                select(Task)
                .where(Task.activity_id == activity.id, Task.status == TaskStatus.PENDING)
                .with_for_update()
            )
        )
        for t in other_pending:
            t.status = TaskStatus.CLOSED
            t.decided_at = utcnow()
        activity.status = ACTIVITY_REJECTED
        instance.reject_reason = reason
        self._audit(
            instance,
            close_event,
            node_id=node["id"],
            actor=actor,
            actor_type=actor_type,
            detail={"closed": [t.assignee for t in other_pending]},
            request_id=request_id,
        )

        on_reject = node.get("on_reject")
        if on_reject:
            self._advance(instance, definition, nodes, on_reject, actor=actor)
        else:
            # 默认：直接终止为 rejected
            instance.status = InstanceStatus.REJECTED
            instance.completed_at = utcnow()
            self._audit(
                instance,
                EventType.COMPLETE,
                node_id=node["id"],
                actor=None,
                actor_type=AuditActorType.SYSTEM,
                detail={"outcome": "rejected", "reason": reason},
                request_id=request_id,
            )

    def _current_pending_activity(self, instance: Instance, node_id: str) -> NodeActivity:
        activity = self.s.scalar(
            select(NodeActivity)
            .where(
                NodeActivity.instance_id == instance.id,
                NodeActivity.node_id == node_id,
                NodeActivity.status == ACTIVITY_PENDING,
            )
            .with_for_update()
        )
        if activity is None:
            raise ConflictError(f"节点 {node_id} 当前没有进行中的审批", code="activity_not_pending")
        return activity

    # ------------------------------------------------------------ 审批决策

    def decide(
        self,
        *,
        instance_id: int,
        definition: dict,
        actor: str,
        action: str,
        task_id: int | None,
        node_id: str | None,
        comment: str | None,
        request_id: str,
    ) -> dict:
        """提交一次审批决定。调用方负责幂等台账与外层事务。"""
        instance = self._lock_instance(instance_id)
        if instance.status != InstanceStatus.RUNNING:
            raise ConflictError(f"流程已结束（{instance.status}），不能再审批", code="instance_not_running")

        nodes = self._node_map(definition)

        # 定位待办：必须同时匹配 instance 与 task，防止替换实例 ID 越权
        stmt = select(Task).where(Task.instance_id == instance.id, Task.status == TaskStatus.PENDING)
        if task_id is not None:
            stmt = stmt.where(Task.id == task_id)
        elif node_id is not None:
            stmt = stmt.where(Task.node_id == node_id, Task.assignee == actor)
        else:
            raise ConflictError("必须提供 task_id 或 node_id", code="missing_target")

        task = self.s.scalar(stmt.with_for_update())
        if task is None:
            # 区分"无权操作他人待办"与"无待办"
            if task_id is not None:
                owned = self.s.get(Task, task_id)
                if owned is not None and owned.instance_id != instance.id:
                    raise ConflictError("待办不属于该流程实例", code="task_instance_mismatch")
                if owned is not None and owned.assignee != actor:
                    raise ConflictError("只能操作分配给自己的待办", code="not_assignee")
            raise ConflictError("当前没有匹配的待审批任务", code="task_not_pending")

        if task.assignee != actor:
            raise ConflictError("只能操作分配给自己的待办", code="not_assignee")

        node = nodes.get(task.node_id)
        if node is None:
            raise ConflictError(f"节点定义缺失: {task.node_id}", code="missing_reference")

        # 先锁活动行（与超时扫描的 instance→activity→task 顺序一致），
        # 保证并发 approve/reject/超时升级在同一审批节点上严格串行
        activity = self.s.get(NodeActivity, task.activity_id, with_for_update=True)
        if activity is None or activity.status != ACTIVITY_PENDING:
            raise ConflictError("该审批节点已结束", code="activity_not_pending")

        # 锁住该活动下全部待办，使并发决定串行化
        all_tasks = list(
            self.s.scalars(
                select(Task)
                .where(Task.activity_id == task.activity_id, Task.status == TaskStatus.PENDING)
                .with_for_update()
            )
        )
        if task not in all_tasks:
            raise ConflictError("该待办已被处理", code="task_not_pending")

        now = utcnow()
        task.status = TaskStatus.APPROVED if action == "approve" else TaskStatus.REJECTED
        task.decided_at = now
        task.comment = comment

        self._audit(
            instance,
            EventType.APPROVE if action == "approve" else EventType.REJECT,
            node_id=node["id"],
            actor=actor,
            actor_type=AuditActorType.USER,
            detail={"mode": node["mode"], "comment": comment},
            request_id=request_id,
        )

        advanced = False
        if action == "reject":
            # 全签 / 任签统一：出现明确拒绝即按 on_reject 处理
            # （_reject_path 内会关闭其余 pending 待办）
            # 先把刚写入的 REJECTED 从"待关闭"集合排除：它已是 REJECTED 而非 PENDING
            self._reject_path(
                instance,
                definition,
                nodes,
                node,
                comment or "拒绝",
                actor=actor,
                actor_type=AuditActorType.USER,
                request_id=request_id,
            )
            advanced = True
        elif node["mode"] == ApprovalMode.ANY:
            # 任签：一人通过即推进，关闭其余待办
            remaining = [t for t in all_tasks if t.id != task.id]
            for t in remaining:
                t.status = TaskStatus.CLOSED
                t.decided_at = now
            activity.status = ACTIVITY_COMPLETED
            self._audit(
                instance,
                EventType.CLOSE_TASKS,
                node_id=node["id"],
                actor=actor,
                actor_type=AuditActorType.USER,
                detail={"closed": [t.assignee for t in remaining], "reason": "any_mode_first_approval"},
                request_id=request_id,
            )
            self._advance(instance, definition, nodes, node["next"], actor=actor)
            advanced = True
        else:
            # 全签：所有人都通过才推进
            if all(t.status == TaskStatus.APPROVED for t in all_tasks):
                activity.status = ACTIVITY_COMPLETED
                self._advance(instance, definition, nodes, node["next"], actor=actor)
                advanced = True
            # 否则停留在节点，等待其余审批人

        return {
            "instance_id": instance.id,
            "status": instance.status,
            "current_node_id": instance.current_node_id,
            "advanced": advanced,
            "request_id": request_id,
        }

    # ------------------------------------------------------------ 撤回

    def withdraw(
        self,
        *,
        instance_id: int,
        definition: dict,
        actor: str,
        comment: str | None,
        request_id: str,
    ) -> dict:
        instance = self._lock_instance(instance_id)
        if actor != instance.submitter:
            # 不泄露实例信息：统一按冲突处理
            raise ConflictError("只有提交人可以撤回", code="not_submitter")
        if instance.status != InstanceStatus.RUNNING:
            raise ConflictError(f"流程已结束（{instance.status}），不能撤回", code="instance_not_running")

        for activity in self._pending_activities(instance, lock=True):
            self._close_pending_tasks(
                instance,
                activity,
                TaskStatus.WITHDRAWN,
                actor=actor,
                actor_type=AuditActorType.SUBMITTER,
                event_type=EventType.WITHDRAW,
                detail={"comment": comment or ""},
                request_id=request_id,
            )
            activity.status = ACTIVITY_WITHDRAWN

        instance.status = InstanceStatus.WITHDRAWN
        instance.completed_at = utcnow()
        self._audit(
            instance,
            EventType.COMPLETE,
            node_id=instance.current_node_id,
            actor=actor,
            actor_type=AuditActorType.SUBMITTER,
            detail={"outcome": "withdrawn", "comment": comment or ""},
            request_id=request_id,
        )
        return {"instance_id": instance.id, "status": instance.status, "request_id": request_id}
