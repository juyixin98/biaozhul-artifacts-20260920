"""
作业结算与节点状态调和。

“完成、取消、超时、心跳恢复并发时只结算一次资源”：
- settle_job() 用条件更新（CAS: status IN ACTIVE_STATUSES）抢占迁移权，
  并发调用中只有一方能把作业带出活动态；删除分配的动作只在 CAS 成功后
  执行，因此资源最多释放一次。终态作业再次调用直接返回 False。
- 所有迁移在单个事务 + 行锁内完成。

节点失联：
- heartbeat 超时的节点转为 offline；失联节点上的资源“不能直接当作可用”，
  其占用保留为事实，同时把上面的活动作业判 FAILED 并释放分配
  （节点恢复前不再把这些卡纳入调度，因为 offline 节点不进候选）。
- 节点恢复上报后重新 available。
"""
from __future__ import annotations

from typing import Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .clock import Clock, get_clock
from .models import Allocation, Job, Node, ResourcePool, SchedulingDecision

CONF = settings.SCHEDULER


# ---------------------------------------------------------------------------
# 作业结算（幂等）
# ---------------------------------------------------------------------------
@transaction.atomic
def settle_job(
    job_id: int,
    target_status: str,
    *,
    reason: str = "",
    clock: Optional[Clock] = None,
) -> bool:
    """
    把一个活动作业结算到终态并释放其全部 GPU 分配。

    成功结算返回 True；作业已处于终态（被并发请求抢先结算）返回 False，
    不做任何重复释放。
    """
    clock = clock or get_clock()
    job = Job.objects.select_for_update().get(pk=job_id)

    if job.status not in Job.ACTIVE_STATUSES:
        # 已经是终态或被抢占：幂等返回，不重复结算。
        return False

    # CAS：行锁 + 状态条件双保险。
    updated = Job.objects.filter(
        id=job.id, status__in=Job.ACTIVE_STATUSES
    ).update(
        status=target_status,
        finished_at=clock.now(),
        status_reason=reason or job.status_reason,
    )
    if updated != 1:  # 极竞情况下被其它事务抢先
        return False

    # 先记录涉及的节点，删除分配后据此刷新 available/occupied。
    affected_node_ids = list(
        Allocation.objects.filter(job=job).values_list("node_id", flat=True)
    )
    released = sum(
        Allocation.objects.filter(job=job).values_list("gpu_count", flat=True)
    )
    Allocation.objects.filter(job=job).delete()

    nodes = list(Node.objects.select_for_update().filter(id__in=affected_node_ids))
    _refresh_nodes(nodes)

    SchedulingDecision.objects.create(
        kind=SchedulingDecision.Kind.SETTLE,
        pool=job.pool,
        job=job,
        rationale={
            "target_status": target_status,
            "reason": reason,
            "released_gpus": released,
        },
        success=True,
        created_at=clock.now(),
    )
    return True


def _used_by_node(node_ids):
    from django.db.models import Sum

    rows = (
        Allocation.objects.filter(node_id__in=list(node_ids))
        .values("node_id")
        .annotate(t=Sum("gpu_count"))
    )
    return {r["node_id"]: r["t"] or 0 for r in rows}


def _refresh_nodes(nodes):
    if not nodes:
        return
    used = _used_by_node([n.id for n in nodes])
    for node in nodes:
        if node.status not in Node.SCHEDULABLE_STATUSES:
            continue
        want = Node.Status.OCCUPIED if used.get(node.id, 0) > 0 else Node.Status.AVAILABLE
        if node.status != want:
            node.status = want
            node.save(update_fields=["status", "updated_at"])


@transaction.atomic
def cancel_job(job_id: int, *, reason: str = "用户取消",
               clock: Optional[Clock] = None) -> bool:
    return settle_job(job_id, Job.Status.CANCELLED, reason=reason, clock=clock)


@transaction.atomic
def complete_job(job_id: int, *, clock: Optional[Clock] = None) -> bool:
    return settle_job(job_id, Job.Status.SUCCEEDED, reason="作业完成", clock=clock)


# ---------------------------------------------------------------------------
# 运行超时
# ---------------------------------------------------------------------------
@transaction.atomic
def timeout_expired_runs(*, clock: Optional[Clock] = None) -> list[int]:
    """把运行超过自身/默认超时的活动作业置为 TIMED_OUT（幂等）。"""
    clock = clock or get_clock()
    default_timeout = CONF["DEFAULT_RUN_TIMEOUT_SECONDS"]
    timed_out: list[int] = []
    jobs = list(
        Job.objects.select_for_update(skip_locked=True).filter(
            status__in=Job.ACTIVE_STATUSES
        )
    )
    now = clock.now()
    for job in jobs:
        start = job.started_at or job.allocated_at
        if start is None:
            continue
        limit = job.run_timeout_seconds or default_timeout
        if limit and (now - start).total_seconds() > limit:
            if settle_job(job.id, Job.Status.TIMED_OUT,
                          reason=f"运行超过 {limit}s 超时", clock=clock):
                timed_out.append(job.id)
    return timed_out


# ---------------------------------------------------------------------------
# 心跳 / 失联 / 恢复 / 排空
# ---------------------------------------------------------------------------
@transaction.atomic
def heartbeat(
    node_id: int,
    *,
    gpu_count: Optional[int] = None,
    gpu_memory_mb: Optional[int] = None,
    clock: Optional[Clock] = None,
) -> Node:
    """
    节点上报心跳与硬件信息。

    - available/occupied/draining 节点刷新心跳时间；
    - offline 节点重新心跳视为“恢复”，回到 available（排空态不会因为
      心跳自动解除，必须显式 activate）。
    硬件信息若发生变化且与现存分配冲突，调用方应先排空；这里仅记录新值。
    """
    clock = clock or get_clock()
    node = Node.objects.select_for_update().get(pk=node_id)
    previous = node.status
    if gpu_count is not None:
        node.gpu_count = gpu_count
    if gpu_memory_mb is not None:
        node.gpu_memory_mb = gpu_memory_mb

    if node.status == Node.Status.OFFLINE:
        node.status = Node.Status.AVAILABLE
        SchedulingDecision.objects.create(
            kind=SchedulingDecision.Kind.NODE_RECOVER,
            pool=node.pool,
            node=node,
            rationale={"from": previous, "to": Node.Status.AVAILABLE},
            created_at=clock.now(),
        )
    node.last_heartbeat_at = clock.now()
    node.save()
    return node


@transaction.atomic
def mark_draining(node_id: int, *, clock: Optional[Clock] = None) -> Node:
    """显式排空：不再接收新任务（不进入调度候选），存量作业跑完。"""
    clock = clock or get_clock()
    node = Node.objects.select_for_update().get(pk=node_id)
    if node.status != Node.Status.OFFLINE:
        node.status = Node.Status.DRAINING
    node.save(update_fields=["status", "updated_at"])
    SchedulingDecision.objects.create(
        kind=SchedulingDecision.Kind.DRAIN,
        pool=node.pool, node=node,
        rationale={"status": node.status},
        created_at=clock.now(),
    )
    return node


@transaction.atomic
def activate_node(node_id: int, *, clock: Optional[Clock] = None) -> Node:
    """解除排空 / 离线隔离：节点重新可调度（需要节点仍有心跳则更合理，
    这里仅做显式恢复；失联检测仍会再次把无心跳节点踢下线）。"""
    clock = clock or get_clock()
    node = Node.objects.select_for_update().get(pk=node_id)
    used = _used_by_node([node.id]).get(node.id, 0)
    node.status = Node.Status.OCCUPIED if used > 0 else Node.Status.AVAILABLE
    node.save(update_fields=["status", "updated_at"])
    SchedulingDecision.objects.create(
        kind=SchedulingDecision.Kind.NODE_RECOVER,
        pool=node.pool, node=node,
        rationale={"manual": True, "to": node.status},
        created_at=clock.now(),
    )
    return node


@transaction.atomic
def detect_stale_nodes(
    *, now=None, clock: Optional[Clock] = None
) -> list[int]:
    """
    把心跳超时的可调度/排空节点标记为 offline，并处理其上的活动作业：
    分配记录被移除（卡不可再调度），作业判 FAILED（失联中止）。

    失联节点的资源不会被当成可用——offline 节点不进入装箱候选。
    """
    clock = clock or get_clock()
    now = now or clock.now()
    timeout = CONF["HEARTBEAT_TIMEOUT_SECONDS"]
    stale_ids: list[int] = []

    nodes = list(
        Node.objects.select_for_update(skip_locked=True).exclude(
            status=Node.Status.OFFLINE
        )
    )
    for node in nodes:
        if node.last_heartbeat_at is None:
            continue
        if (now - node.last_heartbeat_at).total_seconds() > timeout:
            node.status = Node.Status.OFFLINE
            node.save(update_fields=["status", "updated_at"])
            stale_ids.append(node.id)

            # 处理该节点上的活动作业：释放失联卡，作业失败。
            active_jobs = Job.objects.filter(
                allocations__node=node, status__in=Job.ACTIVE_STATUSES
            ).distinct()
            affected = []
            for job in active_jobs:
                # 只释放落在失联节点上的分配；若作业还跨了健康节点，
                # 简化处理仍整体判失败并释放其全部资源（模拟训练中断）。
                affected.append(job.id)
                Allocation.objects.filter(job=job).delete()
                Job.objects.filter(id=job.id, status__in=Job.ACTIVE_STATUSES).update(
                    status=Job.Status.FAILED,
                    finished_at=now,
                    status_reason=f"所在节点 {node.name} 失联，作业中止",
                )
            SchedulingDecision.objects.create(
                kind=SchedulingDecision.Kind.NODE_OFFLINE,
                pool=node.pool, node=node,
                rationale={
                    "last_heartbeat_at": node.last_heartbeat_at.isoformat(),
                    "timeout_seconds": timeout,
                    "affected_jobs": affected,
                },
                success=False,
                created_at=now,
            )
    return stale_ids


# ---------------------------------------------------------------------------
# 服务重启后的恢复
# ---------------------------------------------------------------------------
@transaction.atomic
def recover_on_startup(*, clock: Optional[Clock] = None) -> dict:
    """
    从数据库恢复分配关系。

    Allocation 表本身就是事实来源，无需重建计数。这里做一致性对账：
    - 统计各节点占用，校正 available/occupied；
    - 没有分配却处于 ALLOCATED/RUNNING 的孤儿作业保留原状（等待心跳上报
      或失联检测处理），不臆造资源；
    - 清理任何可能的脏状态（理论上事务保证不存在，仅做巡检并记录）。
    """
    clock = clock or get_clock()
    summary = {"active_jobs": 0, "allocations": 0, "nodes_corrected": 0}

    summary["active_jobs"] = Job.objects.filter(
        status__in=Job.ACTIVE_STATUSES
    ).count()
    summary["allocations"] = Allocation.objects.count()

    nodes = list(Node.objects.exclude(status=Node.Status.OFFLINE))
    used = _used_by_node([n.id for n in nodes])
    for node in nodes:
        if node.status == Node.Status.DRAINING:
            continue
        want = Node.Status.OCCUPIED if used.get(node.id, 0) > 0 else Node.Status.AVAILABLE
        if node.status != want:
            node.status = want
            node.save(update_fields=["status", "updated_at"])
            summary["nodes_corrected"] += 1

    SchedulingDecision.objects.create(
        kind=SchedulingDecision.Kind.NODE_RECOVER,
        rationale={"startup_recovery": True, **summary},
        created_at=clock.now(),
    )
    return summary
