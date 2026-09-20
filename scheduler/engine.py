"""
调度引擎核心。

并发安全设计（多个调度器并发运行也不超卖/不重复分配/不越配额）：
1. 每个资源池先取跨进程互斥的“调度咨询锁”（见 locking.py），同一池的
   调度循环全局串行；
2. 在一个数据库事务里对 resource_pool、候选 node、候选 job 行
   SELECT ... FOR UPDATE，配合状态条件更新（CAS）兜底；
3. GPU“已用量”不保存计数器，每次用 Allocation 实时聚合，避免计数器
   漂移；所有占用写入与状态迁移在同一事务提交。

抢占（优先级 >=8 可抢占 <=3）：
- “先确认受害者释放、再分配”的两阶段在同一事务的 savepoint 中完成；
  若分配阶段失败（注入故障或约束不满足），savepoint 回滚，受害者恢复
  运行、资源不泄漏（失败可恢复），调度继续处理其它作业。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import Sum

from .clock import Clock, get_clock
from .locking import SchedulingLockBusy, pool_scheduling_lock
from .models import (
    Allocation,
    Job,
    Node,
    PreemptionRecord,
    ResourcePool,
    SchedulingDecision,
)
from .packing import NodeCapacity, job_sort_key, plan_allocation

CONF = settings.SCHEDULER


# 测试用故障注入点：抢占释放受害者之后、重新分配装箱之前调用。
# 注入的可调用对象接收 (preemptor, victims)，抛 SchedulingError 即可令
# 本次抢占回滚。生产中保持为空。
PREEMPTION_FAULTS = []


class SchedulingError(Exception):
    """调度/抢占过程中的可恢复错误（savepoint 回滚后状态保持一致）。"""


# ---------------------------------------------------------------------------
# 容量快照（已用量始终实时聚合）
# ---------------------------------------------------------------------------
def _used_gpus_by_node(node_ids: Iterable[int]) -> dict[int, int]:
    ids = list(node_ids)
    if not ids:
        return {}
    rows = (
        Allocation.objects.filter(node_id__in=ids)
        .values("node_id")
        .annotate(total=Sum("gpu_count"))
    )
    return {row["node_id"]: row["total"] or 0 for row in rows}


def _used_of_job(job_id: int) -> int:
    return (
        Allocation.objects.filter(job_id=job_id).aggregate(t=Sum("gpu_count"))["t"]
        or 0
    )


def _candidate_capacities(nodes: list[Node]) -> list[NodeCapacity]:
    used = _used_gpus_by_node([n.id for n in nodes])
    return [
        NodeCapacity(
            node_id=n.id,
            free_gpus=max(n.gpu_count - used.get(n.id, 0), 0),
            total_gpus=n.gpu_count,
            gpu_memory_mb=n.gpu_memory_mb,
            status=n.status,
        )
        for n in nodes
    ]


@dataclass
class ScheduleResult:
    pool_id: int
    scheduled: list[int]
    skipped: list[dict]
    preemptions: list[dict]
    timed_out_queued: list[int]

    def as_dict(self) -> dict:
        return {
            "pool_id": self.pool_id,
            "scheduled": self.scheduled,
            "skipped": self.skipped,
            "preemptions": self.preemptions,
            "timed_out_queued": self.timed_out_queued,
        }


def _log_decision(kind, *, pool=None, job=None, rationale=None,
                  success=True, clock=None):
    return SchedulingDecision.objects.create(
        kind=kind,
        pool=pool,
        job=job,
        rationale=rationale or {},
        success=success,
        created_at=(clock or get_clock()).now(),
    )


# ---------------------------------------------------------------------------
# 排队超时（> 2 小时取消）
# ---------------------------------------------------------------------------
def expire_queued_jobs(pool: ResourcePool, *, clock: Optional[Clock] = None) -> list[int]:
    """把排队超过 QUEUE_TIMEOUT_SECONDS 的作业标记为 TIMED_OUT（幂等）。"""
    clock = clock or get_clock()
    cutoff_ts = clock.now().timestamp() - CONF["QUEUE_TIMEOUT_SECONDS"]
    expired_ids: list[int] = []
    queued = list(
        Job.objects.select_for_update(skip_locked=True).filter(
            pool=pool, status=Job.Status.QUEUED
        )
    )
    for job in queued:
        if job.queued_at.timestamp() <= cutoff_ts:
            job.status = Job.Status.TIMED_OUT
            job.finished_at = clock.now()
            job.status_reason = "排队超过 2 小时未获得资源，自动取消"
            job.save(update_fields=["status", "finished_at", "status_reason",
                                    "updated_at"])
            _log_decision(
                SchedulingDecision.Kind.QUEUE_TIMEOUT,
                pool=pool, job=job,
                rationale={"queued_at": job.queued_at.isoformat(),
                           "timeout_seconds": CONF["QUEUE_TIMEOUT_SECONDS"]},
                clock=clock,
            )
            expired_ids.append(job.id)
    return expired_ids


# ---------------------------------------------------------------------------
# 抢占受害者选择
# ---------------------------------------------------------------------------
def _choose_victims(
    preemptor: Job,
    active_with_used: list[tuple[Job, int]],
    shortage_gpus: int,
    need_slot: bool,
    max_running_jobs: int,
    active_count: int,
) -> list[Job]:
    """
    选择尽量少、尽量弱的低优先级作业，使释放后同时满足：
    - 释放卡数 + 现有空闲 >= 作业 min_gpus；
    - 若并发名额不足，抢占个数让 active_count - chosen + 1 <= 上限。

    排序：优先级升序（最弱先抢）→ 占用卡数升序（少抢为妙）→ id 升序。
    """
    preemptible_limit = CONF["PREEMPTIBLE_PRIORITY"]
    pool = [
        (vj, used)
        for (vj, used) in active_with_used
        if vj.priority <= preemptible_limit and vj.id != preemptor.id
    ]
    pool.sort(key=lambda t: (t[0].priority, t[1], t[0].id))

    chosen: list[Job] = []
    freed = 0
    slots_freed = 0

    def gpu_ok() -> bool:
        return freed >= shortage_gpus

    def slot_ok() -> bool:
        if not need_slot:
            return True
        if max_running_jobs == 0:
            return True
        return active_count - slots_freed < max_running_jobs

    for vj, used in pool:
        if gpu_ok() and slot_ok():
            break
        chosen.append(vj)
        freed += used
        slots_freed += 1

    if not (gpu_ok() and slot_ok()):
        return []  # 即使全部抢占也无法满足
    return chosen


# ---------------------------------------------------------------------------
# 占用写入 / 节点状态翻转
# ---------------------------------------------------------------------------
def _create_allocations(job: Job, plan: dict[int, int]) -> None:
    Allocation.objects.bulk_create(
        [
            Allocation(job=job, node_id=node_id, gpu_count=gpus)
            for node_id, gpus in plan.items()
            if gpus > 0
        ]
    )


def _refresh_node_statuses(nodes: list[Node]) -> None:
    if not nodes:
        return
    used = _used_gpus_by_node([n.id for n in nodes])
    for node in nodes:
        if node.status not in Node.SCHEDULABLE_STATUSES:
            continue
        want = Node.Status.OCCUPIED if used.get(node.id, 0) > 0 else Node.Status.AVAILABLE
        if node.status != want:
            node.status = want
            node.save(update_fields=["status", "updated_at"])


# ---------------------------------------------------------------------------
# 抢占：savepoint 内“释放受害者 + 重新分配”，失败整体回滚
# ---------------------------------------------------------------------------
def _apply_preemption(
    *, pool: ResourcePool, preemptor: Job, victims: list[Job],
    nodes: list[Node], clock: Clock
) -> None:
    savepoint = transaction.savepoint()
    victim_ids = [v.id for v in victims]
    try:
        freed_total = 0
        for victim in victims:
            used = _used_of_job(victim.id)
            updated = Job.objects.filter(
                id=victim.id, status__in=Job.ACTIVE_STATUSES
            ).update(
                status=Job.Status.PREEMPTED,
                finished_at=clock.now(),
                status_reason=f"被高优先级作业 {preemptor.id} 抢占",
            )
            if updated != 1:
                raise SchedulingError(
                    f"受害者作业 {victim.id} 已不再是活动状态，抢占中止"
                )
            Allocation.objects.filter(job_id=victim.id).delete()
            freed_total += used
            PreemptionRecord.objects.create(
                preemptor=preemptor, victim=victim, pool=pool,
                success=True, detail={"freed_gpus": used},
                created_at=clock.now(),
            )

        # 故障注入（仅测试使用）：模拟“释放成功但重新分配阶段失败”。
        for fault in list(PREEMPTION_FAULTS):
            fault(preemptor, victims)

        caps = _candidate_capacities(nodes)
        packing = plan_allocation(
            min_gpus=preemptor.min_gpus,
            max_gpus=preemptor.max_gpus,
            required_memory_mb=preemptor.gpu_memory_mb,
            candidates=caps,
        )
        if not packing["feasible"]:
            raise SchedulingError(f"抢占后资源仍不足以分配：{packing['reason']}")

        requested = packing["allocated_gpus"]
        used_on_schedulable = sum(
            Allocation.objects.filter(node__in=nodes)
            .values_list("gpu_count", flat=True)
        )
        if pool.max_gpus and used_on_schedulable + requested > pool.max_gpus:
            raise SchedulingError("抢占后分配将越过资源池 GPU 配额")

        active_count = Job.objects.filter(
            pool=pool, status__in=Job.ACTIVE_STATUSES
        ).count()
        if pool.max_running_jobs and active_count >= pool.max_running_jobs:
            raise SchedulingError("抢占后并发作业数仍达上限")

        _create_allocations(preemptor, packing["plan"])
        preemptor.status = Job.Status.ALLOCATED
        preemptor.allocated_at = clock.now()
        preemptor.status_reason = f"抢占作业 {victim_ids} 后分配"
        preemptor.save(update_fields=["status", "allocated_at", "status_reason",
                                      "updated_at"])
        _refresh_node_statuses(nodes)
        _log_decision(
            SchedulingDecision.Kind.PREEMPT_RESULT,
            pool=pool, job=preemptor,
            rationale={
                "success": True,
                "victims": victim_ids,
                "freed_gpus": freed_total,
                "plan": {str(k): v for k, v in packing["plan"].items()},
                "allocated_gpus": requested,
            },
            clock=clock,
        )
        transaction.savepoint_commit(savepoint)
    except SchedulingError:
        transaction.savepoint_rollback(savepoint)
        raise
    except DatabaseError as exc:
        transaction.savepoint_rollback(savepoint)
        raise SchedulingError(f"数据库错误，抢占已回滚: {exc}") from exc


def _record_failed_preemption(pool, job, victims, detail, reason, clock) -> None:
    _log_decision(
        SchedulingDecision.Kind.PREEMPT_RESULT,
        pool=pool, job=job, rationale={**detail, "error": reason},
        success=False, clock=clock,
    )
    PreemptionRecord.objects.create(
        preemptor=job, victim=victims[0], pool=pool, success=False,
        detail={**detail, "error": reason, "victim_ids": [v.id for v in victims]},
        created_at=clock.now(),
    )


# ---------------------------------------------------------------------------
# 单个资源池的调度（调用方已持池锁）
# ---------------------------------------------------------------------------
def _schedule_pool_locked(pool_id: int, *, clock: Clock) -> ScheduleResult:
    scheduled: list[int] = []
    skipped: list[dict] = []
    preemptions: list[dict] = []

    with transaction.atomic():
        pool = ResourcePool.objects.select_for_update().get(pk=pool_id)

        timed_out = expire_queued_jobs(pool, clock=clock)

        nodes = list(
            Node.objects.select_for_update().filter(pool=pool).order_by("id")
        )
        schedulable_nodes = [n for n in nodes if n.is_schedulable]

        queued_jobs = list(
            Job.objects.select_for_update(skip_locked=True).filter(
                pool=pool, status=Job.Status.QUEUED
            )
        )
        queued_jobs.sort(key=job_sort_key)

        # 活动作业集合（用于选择抢占受害者）；随着本轮推进同步更新。
        active_jobs: list[Job] = list(
            Job.objects.select_for_update(skip_locked=True).filter(
                pool=pool, status__in=Job.ACTIVE_STATUSES
            )
        )

        for job in queued_jobs:
            caps = _candidate_capacities(schedulable_nodes)
            packing = plan_allocation(
                min_gpus=job.min_gpus,
                max_gpus=job.max_gpus,
                required_memory_mb=job.gpu_memory_mb,
                candidates=caps,
            )
            active_count = Job.objects.filter(
                pool=pool, status__in=Job.ACTIVE_STATUSES
            ).count()
            slot_ok = (
                pool.max_running_jobs == 0
                or active_count < pool.max_running_jobs
            )
            used_on_schedulable = sum(
                Allocation.objects.filter(node__in=schedulable_nodes)
                .values_list("gpu_count", flat=True)
            )

            def skip(reason_text, rationale, success=False):
                skipped.append({"job_id": job.id, "name": job.name,
                                "priority": job.priority, "reason": reason_text})
                _log_decision(
                    SchedulingDecision.Kind.SKIP, pool=pool, job=job,
                    rationale=rationale, success=success, clock=clock,
                )

            need_preempt = (not packing["feasible"]) or (not slot_ok)

            if need_preempt:
                victims: list[Job] = []
                if job.priority >= CONF["PREEMPTION_PRIORITY"]:
                    free_now = sum(c.free_gpus for c in caps)
                    shortage = max(job.min_gpus - free_now, 0)
                    victims = _choose_victims(
                        job,
                        [(v, _used_of_job(v.id)) for v in active_jobs],
                        shortage_gpus=shortage,
                        need_slot=not slot_ok,
                        max_running_jobs=pool.max_running_jobs,
                        active_count=active_count,
                    )

                if not victims:
                    reason = (
                        packing["reason"]
                        if not packing["feasible"]
                        else "资源池并发作业数已达上限且无可抢占目标"
                    )
                    skip(
                        reason,
                        {
                            "reason": reason,
                            "slot_ok": slot_ok,
                            "active_count": active_count,
                            "max_running_jobs": pool.max_running_jobs,
                            "free_gpus": sum(c.free_gpus for c in caps),
                            "candidates": packing["snapshot"],
                            "preemption_allowed":
                                job.priority >= CONF["PREEMPTION_PRIORITY"],
                        },
                    )
                    continue

                detail = {
                    "preemptor": job.id,
                    "victims": [v.id for v in victims],
                    "shortage_gpus": shortage,
                    "need_slot": not slot_ok,
                }
                _log_decision(
                    SchedulingDecision.Kind.PREEMPT_ATTEMPT,
                    pool=pool, job=job, rationale=detail, clock=clock,
                )
                try:
                    _apply_preemption(
                        pool=pool, preemptor=job, victims=victims,
                        nodes=schedulable_nodes, clock=clock,
                    )
                except SchedulingError as exc:
                    _record_failed_preemption(
                        pool, job, victims, detail, str(exc), clock
                    )
                    preemptions.append({
                        "preemptor": job.id,
                        "victims": [v.id for v in victims],
                        "success": False, "reason": str(exc),
                    })
                    skipped.append({
                        "job_id": job.id, "name": job.name,
                        "reason": f"抢占失败已回滚：{exc}",
                    })
                    # 回滚后受害者在 DB 中仍活动；内存对象重新对齐。
                    active_jobs = [
                        v for v in (
                            Job.objects.filter(
                                pool=pool, status__in=Job.ACTIVE_STATUSES
                            )
                        )
                    ]
                    continue

                preemptions.append({
                    "preemptor": job.id, "victims": [v.id for v in victims],
                    "success": True,
                })
                scheduled.append(job.id)
                # 同步内存活动集合：受害者离场，抢占者入场。
                victim_ids = {v.id for v in victims}
                active_jobs = [v for v in active_jobs if v.id not in victim_ids]
                job.status = Job.Status.ALLOCATED
                active_jobs.append(job)
                continue

            # 普通分配路径：校验资源池 GPU 配额。
            requested = packing["allocated_gpus"]
            if pool.max_gpus and used_on_schedulable + requested > pool.max_gpus:
                reason = (
                    f"资源池 GPU 配额将越界：已用 {used_on_schedulable} + "
                    f"申请 {requested} > 配额 {pool.max_gpus}"
                )
                skip(
                    reason,
                    {"reason": reason, "used": used_on_schedulable,
                     "requested": requested, "max_gpus": pool.max_gpus},
                )
                continue

            _create_allocations(job, packing["plan"])
            job.status = Job.Status.ALLOCATED
            job.allocated_at = clock.now()
            job.status_reason = "调度器装箱分配"
            job.save(update_fields=["status", "allocated_at", "status_reason",
                                    "updated_at"])
            _refresh_node_statuses(schedulable_nodes)
            active_jobs.append(job)
            _log_decision(
                SchedulingDecision.Kind.SCHEDULE,
                pool=pool, job=job,
                rationale={
                    "plan": {str(k): v for k, v in packing["plan"].items()},
                    "allocated_gpus": requested,
                    "reason": packing["reason"],
                    "candidates": packing["snapshot"],
                    "active_count_after": active_count + 1,
                },
                clock=clock,
            )
            scheduled.append(job.id)

    return ScheduleResult(
        pool_id=pool_id,
        scheduled=scheduled,
        skipped=skipped,
        preemptions=preemptions,
        timed_out_queued=timed_out,
    )


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def schedule_pool(pool_id: int, *, clock: Optional[Clock] = None,
                  lock_timeout: float = 10.0) -> Optional[ScheduleResult]:
    """
    运行某资源池的一轮调度。拿不到池锁（其它调度器正在调度）返回 None，
    调用方安全跳过——绝不绕过锁强行分配。
    """
    clock = clock or get_clock()
    try:
        with pool_scheduling_lock(pool_id, timeout_seconds=lock_timeout):
            return _schedule_pool_locked(pool_id, clock=clock)
    except SchedulingLockBusy:
        return None


def schedule_all_pools(*, clock: Optional[Clock] = None) -> list[ScheduleResult]:
    results: list[ScheduleResult] = []
    for pool_id in ResourcePool.objects.values_list("id", flat=True).order_by("id"):
        result = schedule_pool(pool_id, clock=clock)
        if result is not None:
            results.append(result)
    return results
