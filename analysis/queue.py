"""
Database-backed task queue protocol.

Claim semantics (MySQL, the deployment target)
----------------------------------------------
* Claim: one transaction, `SELECT ... FOR UPDATE SKIP LOCKED` over the
  oldest PENDING row OR a RUNNING row whose lease has expired. Two workers
  therefore never receive the same task.
* Each (re)claim bumps `generation`. The worker must carry that generation
  (plus its worker_id) on every later write.
* Heartbeat: advances lease_expires_at; refused if the worker lost the lease.
* Complete/Fail: guarded by (worker_id, generation, status='running'). A
  zombie worker that paused past its lease, had the task reclaimed and
  redone, then wakes up — its writes are rejected, so it cannot overwrite
  the newer worker's result.
* Failures: requeued up to max_attempts; the final attempt marks the task
  FAILED permanently with error_stage/error_message preserved. One failed
  document never affects the other documents in a batch.

SQLite
------
`SKIP LOCKED` does not exist there, so claim uses a plain `FOR UPDATE`
(Django turns it into a deferred no-op) and concurrent-claim guarantees are
tested against MySQL only. Queue ordering/lease/retry logic is still fully
exercised on SQLite.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import AnalysisTask

logger = logging.getLogger(__name__)


def _supports_skip_locked() -> bool:
    from django.db import connection

    return connection.vendor == "mysql"


def _locked_qs():
    """FOR UPDATE row lock; SKIP LOCKED on MySQL so workers never queue
    behind one another's locked rows."""
    return AnalysisTask.objects.select_for_update(
        skip_locked=_supports_skip_locked()
    )


@transaction.atomic
def claim_task(lease_seconds: int, max_attempts: int,
               document_ids=None) -> AnalysisTask | None:
    """Atomically claim one task; returns None when the queue is empty.

    Selects the oldest PENDING task first (FIFO), then an expired RUNNING
    task. The row lock (FOR UPDATE SKIP LOCKED on MySQL) makes concurrent
    workers partition the queue instead of racing. `document_ids` optionally
    restricts the claim (used by the synchronous demo seeder).
    """
    now = timezone.now()

    def _scope(qs):
        if document_ids is not None:
            qs = qs.filter(document_id__in=document_ids)
        return qs

    task = _scope(_locked_qs()).filter(status=AnalysisTask.Status.PENDING).first()
    if task is None:
        task = (
            _scope(_locked_qs())
            .filter(
                status=AnalysisTask.Status.RUNNING,
                lease_expires_at__lt=now,
            )
            .first()
        )
    if task is None:
        return None

    reclaim = task.status == AnalysisTask.Status.RUNNING
    task.attempts += 1
    task.generation += 1
    task.status = AnalysisTask.Status.RUNNING
    task.stage = AnalysisTask.Stage.EXTRACT
    task.worker_id = uuid.uuid4().hex
    task.leased_at = now
    task.lease_expires_at = now + timedelta(seconds=lease_seconds)
    task.started_at = task.started_at or now
    task.error_stage = ""
    task.error_message = ""
    if reclaim:
        logger.warning(
            "Reclaimed expired task %s after %s attempts (new worker=%s)",
            task.pk, task.attempts, task.worker_id,
        )
    task.save()
    return task


@transaction.atomic
def heartbeat(task_id: int, worker_id: str, generation: int,
              lease_seconds: int) -> bool:
    """Extend a lease. Returns False if the worker no longer owns the task."""
    qs = _locked_qs()
    try:
        task = qs.get(pk=task_id)
    except AnalysisTask.DoesNotExist:
        return False
    if not _still_owner(task, worker_id, generation):
        return False
    task.lease_expires_at = timezone.now() + timedelta(seconds=lease_seconds)
    task.save(update_fields=["lease_expires_at", "updated_at"])
    return True


@transaction.atomic
def set_stage(task_id: int, worker_id: str, generation: int, stage: str) -> bool:
    """Record the current pipeline stage (for locatable failure reporting)."""
    qs = _locked_qs()
    try:
        task = qs.get(pk=task_id)
    except AnalysisTask.DoesNotExist:
        return False
    if not _still_owner(task, worker_id, generation):
        return False
    task.stage = stage
    task.save(update_fields=["stage", "updated_at"])
    return True


def _still_owner(task: AnalysisTask, worker_id: str, generation: int) -> bool:
    return (
        task.status == AnalysisTask.Status.RUNNING
        and task.worker_id == worker_id
        and task.generation == generation
    )


@transaction.atomic
def complete_task(task_id: int, worker_id: str, generation: int,
                  result_writer) -> bool:
    """
    Mark a task succeeded and persist the result IN THE SAME TRANSACTION.

    `result_writer(task)` is called while the row is locked and must create
    the AnalysisResult (task=task). If it raises, the task is failed instead.
    The ownership guard means only the current lease holder can ever write.
    """
    qs = _locked_qs()
    task = qs.get(pk=task_id)
    if not _still_owner(task, worker_id, generation):
        logger.warning(
            "Refused completion of task %s from stale worker %s "
            "(current worker=%s gen=%s, submitted gen=%s)",
            task_id, worker_id, task.worker_id, task.generation, generation,
        )
        return False
    result_writer(task)
    task.status = AnalysisTask.Status.SUCCEEDED
    task.stage = AnalysisTask.Stage.PERSIST
    task.finished_at = timezone.now()
    task.lease_expires_at = None
    task.error_stage = ""
    task.error_message = ""
    task.save()
    return True


@transaction.atomic
def fail_task(task_id: int, worker_id: str, generation: int,
              stage: str, message: str, max_attempts: int) -> str:
    """
    Record a failure. Retryable while attempts < max_attempts (back to
    PENDING with the lease cleared); otherwise the task is FAILED terminally.
    Never raises for stale workers — just reports ownership lost.
    """
    qs = _locked_qs()
    try:
        task = qs.get(pk=task_id)
    except AnalysisTask.DoesNotExist:
        return "missing"
    if not _still_owner(task, worker_id, generation):
        logger.warning(
            "Refused failure-report for task %s from stale worker %s",
            task_id, worker_id,
        )
        return "stale"

    task.error_stage = stage
    # Cap stored message length; the full traceback is logged separately.
    task.error_message = message[:4000]
    if task.attempts >= min(max_attempts, task.max_attempts):
        task.status = AnalysisTask.Status.FAILED
        task.finished_at = timezone.now()
        task.lease_expires_at = None
        task.save()
        return "failed"
    # Requeue for another worker (or this one on its next poll).
    task.status = AnalysisTask.Status.PENDING
    task.stage = AnalysisTask.Stage.QUEUED
    task.worker_id = ""
    task.leased_at = None
    task.lease_expires_at = None
    task.save()
    return "requeued"


def queue_depth() -> dict:
    return {
        "pending": AnalysisTask.objects.filter(status=AnalysisTask.Status.PENDING).count(),
        "running": AnalysisTask.objects.filter(status=AnalysisTask.Status.RUNNING).count(),
        "succeeded": AnalysisTask.objects.filter(status=AnalysisTask.Status.SUCCEEDED).count(),
        "failed": AnalysisTask.objects.filter(status=AnalysisTask.Status.FAILED).count(),
    }
