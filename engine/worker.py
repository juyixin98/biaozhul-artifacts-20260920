"""工作进程主循环：领取 → 心跳续租 → 执行管线 → fence 保护下落库。

多个 worker 进程可同时运行；所有正确性保证见 ``queue`` 模块文档。
"""

import threading
import time
import traceback

from django.conf import settings
from django.db import close_old_connections, transaction

from .analysis.pipeline import (
    StageError,
    finalize_metrics,
    stage_extract,
    stage_metrics,
    stage_nlp,
    stage_style,
)
from .analysis.version import METHODOLOGY
from .models import AnalysisResult, AnalysisTask, Submission
from . import queue


def _safe_progress(task, worker_id, run_token, hb, stage, progress):
    """更新阶段进度前先确认仍持有租约；丢失租约立即中止执行。"""
    hb.check()
    if not queue.update_progress(task.id, worker_id, run_token, stage, progress):
        raise LostLeaseError("进度更新被拒：任务已不属于当前 worker。")


class LostLeaseError(Exception):
    """心跳发现任务已被回收：worker 必须放弃本次执行的一切写入。"""


class LeaseHeartbeat(threading.Thread):
    daemon = True

    def __init__(self, task_id, worker_id, run_token, lease_seconds):
        super().__init__()
        self.task_id = task_id
        self.worker_id = worker_id
        self.run_token = run_token
        self.lease_seconds = lease_seconds
        self.stop = threading.Event()
        self.lost = False

    def run(self):
        interval = max(1.0, self.lease_seconds / 3.0)
        while not self.stop.wait(interval):
            close_old_connections()
            ok = queue.heartbeat(
                self.task_id, self.worker_id, self.run_token, self.lease_seconds
            )
            if not ok:
                self.lost = True
                return

    def check(self):
        if self.lost:
            raise LostLeaseError("任务租约已丢失（可能已被回收并转交）。")


@transaction.atomic
def _persist_result(task, worker_id, run_token, metrics, methodology, input_hash):
    """结果落库 + 成功终态，在同一事务内再次做 fence 校验。

    版本号按 submission 现有最大版本 +1 分配；
    ``uniq_result_version_per_submission`` 约束兜底防重复版本。
    """
    locked = (
        AnalysisTask.objects.select_for_update()
        .filter(id=task.id, worker_id=worker_id, run_token=run_token)
        .first()
    )
    if locked is None or not queue_mod_owns(locked):
        return False

    # 锁 submission 行，串行化同一文档并发重跑时的版本号分配，
    # 避免两个 worker 同时选中同一个 next_version。
    Submission.objects.select_for_update().filter(id=task.submission_id).first()
    last = (
        AnalysisResult.objects.filter(submission_id=task.submission_id)
        .order_by("-version")
        .first()
    )
    next_version = (last.version + 1) if last else 1
    AnalysisResult.objects.create(
        submission_id=task.submission_id,
        task_id=task.id,
        version=next_version,
        input_hash=input_hash,
        algorithm_version=methodology["algorithm_version"],
        metrics=metrics,
        methodology=methodology,
    )
    locked.status = AnalysisTask.Status.SUCCEEDED
    locked.progress = 100
    locked.stage = AnalysisTask.Stage.PERSIST
    locked.lease_expires_at = None
    locked.finished_at = queue.now()
    locked.error_code = ""
    locked.error_message = ""
    locked.save()
    return True


def queue_mod_owns(task):
    return queue._owns(task, task.worker_id, task.run_token)


def execute_task(task, worker_id, lease_seconds):
    """执行单个已领取任务；返回 'succeeded' | 'retry' | 'failed' | 'lost'。"""
    hb = LeaseHeartbeat(task.id, worker_id, task.run_token, lease_seconds)
    hb.start()
    current_stage = AnalysisTask.Stage.EXTRACT
    try:
        hb.check()
        # 分阶段执行，阶段间在 fence 保护下写入进度。
        text, input_hash = stage_extract(task.submission)
        current_stage = AnalysisTask.Stage.NLP
        _safe_progress(task, worker_id, task.run_token, hb,
                       AnalysisTask.Stage.NLP, 30)

        doc = stage_nlp(text)
        current_stage = AnalysisTask.Stage.METRICS
        _safe_progress(task, worker_id, task.run_token, hb,
                       AnalysisTask.Stage.METRICS, 55)

        result_metrics = stage_metrics(text, doc)
        current_stage = AnalysisTask.Stage.STYLE
        _safe_progress(task, worker_id, task.run_token, hb,
                       AnalysisTask.Stage.STYLE, 75)

        style = stage_style(task.submission, doc, input_hash)
        finalize_metrics(result_metrics, doc, style)
        result_metrics["char_count"] = len(text)

        current_stage = AnalysisTask.Stage.PERSIST
        _safe_progress(task, worker_id, task.run_token, hb,
                       AnalysisTask.Stage.PERSIST, 90)

        ok = _persist_result(
            task, worker_id, task.run_token,
            result_metrics, METHODOLOGY, input_hash,
        )
        if not ok:
            return "lost"
        return "succeeded"
    except LostLeaseError:
        return "lost"
    except StageError as exc:
        accepted = queue.finish_failure(
            task.id, worker_id, task.run_token, exc.stage, exc.code, exc.message, exc.detail
        )
        if not accepted:
            return "lost"
        return _terminal_state(task.id)
    except Exception as exc:
        detail = {"traceback": traceback.format_exc(limit=5)}
        try:
            accepted = queue.finish_failure(
                task.id, worker_id, task.run_token,
                current_stage, "UNEXPECTED", f"{type(exc).__name__}: {exc}", detail,
            )
            return "lost" if not accepted else _terminal_state(task.id)
        except LostLeaseError:
            return "lost"
    finally:
        hb.stop.set()


def _terminal_state(task_id):
    task = AnalysisTask.objects.get(id=task_id)
    return "failed" if task.status == AnalysisTask.Status.FAILED else "retry"


class Worker:
    def __init__(self, worker_id=None, lease_seconds=None, poll_interval=None,
                 once=False, max_tasks=0):
        self.worker_id = worker_id or queue.generate_worker_id()
        self.lease_seconds = lease_seconds or settings.TASK_LEASE_SECONDS
        self.poll_interval = poll_interval or settings.TASK_POLL_INTERVAL
        self.once = once          # 处理至多一个任务（无任务也返回）
        self.max_tasks = max_tasks  # >0 时处理满 N 个任务后退出
        self._running = True
        self._tasks_done = 0

    def shutdown(self, *args):
        self._running = False

    def run(self):
        while self._running:
            close_old_connections()
            try:
                # 每个轮次先回收过期租约（多 worker 下该操作幂等）。
                queue.reclaim_expired()
                task = queue.claim_task(self.worker_id, self.lease_seconds)
                if task is None:
                    if self.once:
                        return None
                    time.sleep(self.poll_interval)
                    continue
                outcome = execute_task(task, self.worker_id, self.lease_seconds)
                self._tasks_done += 1
                if self.once:
                    return outcome
                if self.max_tasks and self._tasks_done >= self.max_tasks:
                    return outcome
            except Exception:
                # 轮询/回收层异常不应杀死 worker；打印后继续。
                traceback.print_exc()
                time.sleep(self.poll_interval)
