"""数据库任务队列。

并发安全设计
============
1. **领取**：单条条件 UPDATE（``status=PENDING AND (not_before 为空或到期)``）
   配合行锁完成“谁抢到谁拥有”，MySQL 行锁 / SQLite 写锁天然互斥；
   领取即设置 worker_id、租约时间和新的 run_token（fence token）。
2. **租约与回收**：worker 定期续租；崩溃后租约过期，任务可被重新领取，
   attempts 累加；达到上限标记 FAILED。
3. **防旧 worker 覆盖**：finish/heartbeat 的条件中带 ``run_token``。
   任务被回收并由新 worker 领取后 run_token 变化，旧 worker 即使恢复，
   其续租和结果提交都会被拒绝（影响行数为 0）。
"""

import uuid

from django.db import transaction
from django.utils import timezone

from .models import AnalysisTask


def now():
    return timezone.now()


def generate_worker_id():
    return uuid.uuid4().hex[:12]


@transaction.atomic
def claim_task(worker_id, lease_seconds):
    """原子领取一个到期任务；无任务返回 None。

    用 select_for_update(skip_locked) 锁候选行，再条件更新，避免多 worker
    反复争抢同一行；MySQL 与较新 SQLite 均支持该语义。
    """
    from django.db.models import Q

    due = Q(not_before__isnull=True) | Q(not_before__lte=now())
    qs = AnalysisTask.objects.select_for_update(skip_locked=True).filter(
        status=AnalysisTask.Status.PENDING
    ).filter(due)
    candidate = qs.order_by("id").first()
    if candidate is None:
        return None

    candidate.status = AnalysisTask.Status.RUNNING
    candidate.stage = AnalysisTask.Stage.EXTRACT
    candidate.progress = 5
    candidate.worker_id = worker_id
    candidate.attempts += 1
    candidate.run_token += 1
    candidate.lease_expires_at = now() + timezone.timedelta(seconds=lease_seconds)
    candidate.started_at = candidate.started_at or now()
    candidate.not_before = None
    candidate.save()
    return candidate


@transaction.atomic
def heartbeat(task_id, worker_id, run_token, lease_seconds):
    """续租。任务被回收/转交后旧 worker 的心跳返回 False。"""
    updated = AnalysisTask.objects.filter(
        id=task_id,
        status=AnalysisTask.Status.RUNNING,
        worker_id=worker_id,
        run_token=run_token,
    ).update(lease_expires_at=now() + timezone.timedelta(seconds=lease_seconds))
    return updated == 1


@transaction.atomic
def update_progress(task_id, worker_id, run_token, stage, progress):
    """更新阶段/进度；必须仍持有有效租约与 fence token。"""
    task = AnalysisTask.objects.select_for_update().filter(id=task_id).first()
    if task is None:
        return False
    if (
        task.status != AnalysisTask.Status.RUNNING
        or task.worker_id != worker_id
        or task.run_token != run_token
    ):
        return False
    if task.lease_expires_at is not None and task.lease_expires_at < now():
        # 租约已过期：本 worker 已失去任务所有权，不得再写。
        return False
    task.stage = stage
    task.progress = progress
    task.save()
    return True


@transaction.atomic
def finish_success(task_id, worker_id, run_token):
    """成功落终态。fence 校验失败返回 False，调用方必须放弃提交。"""
    task = AnalysisTask.objects.select_for_update().filter(id=task_id).first()
    if task is None or not _owns(task, worker_id, run_token):
        return False
    task.status = AnalysisTask.Status.SUCCEEDED
    task.progress = 100
    task.stage = AnalysisTask.Stage.PERSIST
    task.lease_expires_at = None
    task.finished_at = now()
    task.error_code = ""
    task.error_message = ""
    task.save()
    return True


@transaction.atomic
def finish_failure(task_id, worker_id, run_token, stage, code, message, detail=None):
    """单次尝试失败：未超上限则回到 PENDING（指数退避），否则 FAILED 终态。"""
    from django.conf import settings

    task = AnalysisTask.objects.select_for_update().filter(id=task_id).first()
    if task is None or not _owns(task, worker_id, run_token):
        # 旧 worker 的失败上报直接丢弃，避免覆盖新执行。
        return False
    task.error_code = code
    task.error_message = message
    task.error_detail = {"stage": stage, **(detail or {})}
    task.lease_expires_at = None
    task.finished_at = now()
    if task.attempts >= task.max_attempts:
        task.status = AnalysisTask.Status.FAILED
        task.progress = 0
    else:
        backoff = settings.TASK_RETRY_BACKOFF_BASE * (2 ** (task.attempts - 1))
        task.status = AnalysisTask.Status.PENDING
        task.stage = stage
        task.worker_id = ""
        task.not_before = now() + timezone.timedelta(seconds=backoff)
    task.save()
    return True


@transaction.atomic
def reclaim_expired(lease_grace=0):
    """回收租约过期的 RUNNING 任务。

    未超尝试上限：回到 PENDING 并施加与失败重试一致的指数退避
    （下次领取时 attempts 才递增，run_token 也在领取时递增，
    使旧 worker 的后续写入全部失效）。超过上限直接 FAILED。
    """
    from django.conf import settings

    cutoff = now() - timezone.timedelta(seconds=lease_grace)
    qs = AnalysisTask.objects.select_for_update(skip_locked=True).filter(
        status=AnalysisTask.Status.RUNNING, lease_expires_at__lt=cutoff
    )
    reclaimed = []
    for task in qs:
        previous = task.error_detail.get("lease_timeouts", 0)
        task.error_detail = {**task.error_detail, "lease_timeouts": previous + 1}
        task.worker_id = ""
        task.lease_expires_at = None
        if task.attempts >= task.max_attempts:
            task.status = AnalysisTask.Status.FAILED
            task.error_code = "LEASE_EXPIRED_MAX_ATTEMPTS"
            task.error_message = "租约多次过期，超过最大尝试次数。"
            task.finished_at = now()
        else:
            backoff = settings.TASK_RETRY_BACKOFF_BASE * (2 ** (task.attempts - 1))
            task.status = AnalysisTask.Status.PENDING
            task.not_before = now() + timezone.timedelta(seconds=backoff)
        task.save()
        reclaimed.append(task.id)
    return reclaimed


def _owns(task, worker_id, run_token):
    if task.status != AnalysisTask.Status.RUNNING:
        return False
    if task.worker_id != worker_id or task.run_token != run_token:
        return False
    if task.lease_expires_at is not None and task.lease_expires_at < now():
        return False
    return True
