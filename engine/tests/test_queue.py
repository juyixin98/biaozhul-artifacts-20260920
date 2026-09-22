"""队列：并发领取、租约过期回收、fence 防旧 worker 覆盖、失败重试。"""

from datetime import timedelta

from django.test import TransactionTestCase
from django.utils import timezone

from engine import queue
from engine.models import AnalysisResult, AnalysisTask
from engine.worker import LostLeaseError, execute_task

from .factories import make_course, make_submission, make_task


class ClaimTests(TransactionTestCase):
    def test_concurrent_claim_no_duplicate(self):
        """两个 worker 同时领取，只能有一个拿到任务。"""
        course = make_course()
        task = make_task(make_submission(course))
        w1 = queue.claim_task("worker-1", lease_seconds=60)
        w2 = queue.claim_task("worker-2", lease_seconds=60)
        self.assertEqual(w1.id, task.id)
        self.assertIsNone(w2)
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTask.Status.RUNNING)
        self.assertEqual(task.worker_id, "worker-1")
        self.assertEqual(task.attempts, 1)
        self.assertEqual(task.run_token, 1)

    def test_lease_expired_reclaimable(self):
        """租约过期后任务可被回收并被新 worker 领取，attempts 累加。"""
        course = make_course()
        task = make_task(make_submission(course))
        t1 = queue.claim_task("old-worker", lease_seconds=60)
        old_token = t1.run_token

        # 模拟租约过期
        AnalysisTask.objects.filter(id=task.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        reclaimed = queue.reclaim_expired()
        self.assertIn(task.id, reclaimed)
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTask.Status.PENDING)
        self.assertEqual(task.worker_id, "")
        # 回收后处于指数退避；清除退避后可被新 worker 立即领取
        self.assertGreater(task.not_before, timezone.now())
        AnalysisTask.objects.filter(id=task.id).update(not_before=None)

        t2 = queue.claim_task("new-worker", lease_seconds=60)
        self.assertEqual(t2.id, task.id)
        self.assertEqual(t2.attempts, 2)
        self.assertGreater(t2.run_token, old_token)

    def test_stale_worker_cannot_heartbeat_or_finish(self):
        """旧 worker 恢复后：心跳被拒、失败上报被拒，无法覆盖新执行。"""
        course = make_course()
        task = make_task(make_submission(course))
        stale = queue.claim_task("old-worker", lease_seconds=60)
        stale_token = stale.run_token
        AnalysisTask.objects.filter(id=task.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        queue.reclaim_expired()
        AnalysisTask.objects.filter(id=task.id).update(not_before=None)
        new = queue.claim_task("new-worker", lease_seconds=60)

        # 旧 worker 心跳
        self.assertFalse(
            queue.heartbeat(task.id, "old-worker", stale_token, lease_seconds=60)
        )
        # 旧 worker 尝试上报失败（不应把新执行的任务改回 PENDING/FAILED）
        accepted = queue.finish_failure(
            task.id, "old-worker", stale_token, "NLP", "BOGUS", "旧 worker 迟到的上报"
        )
        self.assertFalse(accepted)
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTask.Status.RUNNING)
        self.assertEqual(task.worker_id, "new-worker")
        self.assertEqual(new.run_token, task.run_token)

    def test_successful_finish_is_fenced_once(self):
        """正常完成后结果只落一次；重复 finish 返回 False。"""
        course = make_course()
        task = make_task(make_submission(course, text="Another short text here."))
        claimed = queue.claim_task("w", lease_seconds=60)
        outcome = execute_task(claimed, "w", lease_seconds=60)
        self.assertEqual(outcome, "succeeded")
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTask.Status.SUCCEEDED)
        self.assertEqual(AnalysisResult.objects.filter(submission=task.submission).count(), 1)
        # 再次完成不可能成功
        self.assertFalse(queue.finish_success(task.id, "w", task.run_token))

    def test_retry_backoff_then_fail_after_max_attempts(self):
        """失败按指数退避重试；达到上限进入 FAILED 终态并保留阶段与错误。"""
        course = make_course()
        # 空字节 → EXTRACT 阶段必然失败
        submission = make_submission(course, text="x")
        submission.raw_content = b""
        submission.save()
        task = make_task(submission)
        task.max_attempts = 2
        task.save()

        first = queue.claim_task("w1", lease_seconds=60)
        outcome = execute_task(first, "w1", 60)
        self.assertEqual(outcome, "retry")
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTask.Status.PENDING)
        self.assertEqual(task.error_detail["stage"], "EXTRACT")
        self.assertTrue(task.error_code)
        self.assertIsNotNone(task.not_before)
        self.assertGreater(task.not_before, timezone.now())

        # 退避未到期不能被领取
        self.assertIsNone(queue.claim_task("w2", lease_seconds=60))
        # 到期后重试
        AnalysisTask.objects.filter(id=task.id).update(not_before=None)
        second = queue.claim_task("w2", lease_seconds=60)
        outcome = execute_task(second, "w2", 60)
        self.assertEqual(outcome, "failed")
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTask.Status.FAILED)
        self.assertEqual(task.attempts, 2)
        self.assertEqual(task.error_detail["stage"], "EXTRACT")


class LeaseTimeoutMaxTests(TransactionTestCase):
    def test_lease_timeouts_exhausting_attempts_fail_task(self):
        course = make_course()
        task = make_task(make_submission(course))
        task.max_attempts = 1
        task.save()
        claimed = queue.claim_task("w", lease_seconds=60)
        AnalysisTask.objects.filter(id=task.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        queue.reclaim_expired()
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTask.Status.FAILED)
        self.assertEqual(task.error_code, "LEASE_EXPIRED_MAX_ATTEMPTS")

    def test_backoff_does_not_block_later_due_task(self):
        """任务 A 在退避（not_before 未到期）时，后续到期任务 B 仍可被领取。"""
        course = make_course()
        a = make_task(make_submission(course, text="Task alpha body for testing."))
        b = make_task(make_submission(course, text="Task beta body for testing."))
        # 令 A 进入退避
        claimed_a = queue.claim_task("w", lease_seconds=60)
        AnalysisTask.objects.filter(id=a.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        queue.reclaim_expired()
        a.refresh_from_db()
        self.assertEqual(a.status, AnalysisTask.Status.PENDING)
        self.assertIsNotNone(a.not_before)
        self.assertGreater(a.not_before, timezone.now())

        # 队列头是 A，但 B 应可被领取
        picked = queue.claim_task("w2", lease_seconds=60)
        self.assertEqual(picked.id, b.id)
