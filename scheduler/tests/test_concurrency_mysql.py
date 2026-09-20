"""
MySQL 多连接并发测试。

这些测试用真实线程 + 独立数据库连接验证：
- 多个调度器并发运行不超卖 GPU、不重复分配、不越过资源池配额；
- 完成 / 取消 / 心跳恢复并发时资源只结算一次；
- 抢占失败（注入故障）后数据库状态一致、可恢复；
- 节点失联与调度并发时失联资源不会被当成可用；
- 排队超时与调度并发安全。

使用 TransactionTestCase（每线程独立连接、线程内自行清理）。
当默认数据库不是 MySQL（例如 SQLite）时整模块跳过——SQLite 无法提供
跨连接行锁/咨询锁语义。
运行方式见 README：docker compose up db 后
MYSQL_HOST=127.0.0.1 python manage.py test scheduler.tests.test_concurrency_mysql
"""
from __future__ import annotations

import threading
import traceback
from datetime import timedelta

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from scheduler import engine, reconcile
from scheduler.models import Allocation, Job, Node, ResourcePool


def _is_mysql() -> bool:
    return connection.vendor == "mysql"


def _run_in_threads(targets, *, timeout=60):
    """启动若干线程，每个 target 无参数；收集异常。"""
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(targets))

    def wrapper(fn):
        try:
            barrier.wait()
            fn()
        except BaseException as exc:  # noqa: BLE001 - 测试要捕获一切
            errors.append(exc)
            traceback.print_exc()
        finally:
            connection.close()

    threads = [threading.Thread(target=wrapper, args=(t,)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
        if t.is_alive():
            raise AssertionError("线程超时未结束（可能死锁）")
    return errors


def _total_used(pool_id: int) -> int:
    return sum(
        Allocation.objects.filter(node__pool_id=pool_id)
        .values_list("gpu_count", flat=True)
    )


def _active_count(pool_id: int) -> int:
    return Job.objects.filter(
        pool_id=pool_id, status__in=Job.ACTIVE_STATUSES
    ).count()


class MySQLMixin:
    def _skip_if_not_mysql(self):
        if not _is_mysql():
            self.skipTest("并发测试需要 MySQL（使用 DB_ENGINE=mysql）")


class ConcurrentSchedulerTests(TransactionTestCase, MySQLMixin):
    def setUp(self):
        self._skip_if_not_mysql()
        self.pool = ResourcePool.objects.create(
            name="conc-pool", max_gpus=16, max_running_jobs=16
        )
        # 2 个 8 卡节点 = 16 卡
        for i in (1, 2):
            node = Node.objects.create(
                pool=self.pool, name=f"c-node-{i}",
                gpu_count=8, gpu_memory_mb=16000,
                status=Node.Status.AVAILABLE,
            )
            node.last_heartbeat_at = timezone.now()
            node.save()

        # 32 个 1 卡作业：容量只够 16 个。
        self.jobs = [
            Job.objects.create(
                pool=self.pool, name=f"j-{i}",
                min_gpus=1, max_gpus=1, gpu_memory_mb=0,
                priority=5, status=Job.Status.QUEUED,
            )
            for i in range(32)
        ]

    def test_four_schedulers_no_oversell(self):
        def loop():
            connection.close()  # 强制线程独立连接
            for _ in range(5):
                engine.schedule_pool(self.pool.id, lock_timeout=30)

        errors = _run_in_threads([loop, loop, loop, loop])
        self.assertEqual(errors, [])

        # 恰好 16 个作业被分配，其余继续排队
        allocated = Job.objects.filter(
            pool=self.pool, status=Job.Status.ALLOCATED
        ).count()
        queued = Job.objects.filter(
            pool=self.pool, status=Job.Status.QUEUED
        ).count()
        self.assertEqual(allocated, 16)
        self.assertEqual(queued, 16)

        # 物理上不超卖
        self.assertLessEqual(_total_used(self.pool.id), 16)
        self.assertEqual(_total_used(self.pool.id), 16)

        # 每个作业最多只有一份分配，且无节点超容量
        counts = {
            n.id: sum(
                Allocation.objects.filter(node_id=n.id)
                .values_list("gpu_count", flat=True)
            )
            for n in Node.objects.filter(pool=self.pool)
        }
        for node_id, used in counts.items():
            self.assertLessEqual(used, 8, f"节点 {node_id} 超卖: {used}")

        # 每个作业的分配行数合理（活动作业恰好 1 行）
        active_ids = list(
            Job.objects.filter(
                pool=self.pool, status__in=Job.ACTIVE_STATUSES
            ).values_list("id", flat=True)
        )
        for job_id in active_ids:
            n_rows = Allocation.objects.filter(job_id=job_id).count()
            self.assertEqual(n_rows, 1, f"作业 {job_id} 被重复分配")

    def test_pool_concurrency_quota_under_contention(self):
        """资源池并发作业上限在多调度器竞争下不被突破。"""
        pool = ResourcePool.objects.create(
            name="quota-pool", max_gpus=100, max_running_jobs=3
        )
        node = Node.objects.create(
            pool=pool, name="q-node", gpu_count=64, gpu_memory_mb=1000,
            status=Node.Status.AVAILABLE,
        )
        node.last_heartbeat_at = timezone.now()
        node.save()
        jobs = [
            Job.objects.create(
                pool=pool, name=f"qj-{i}", min_gpus=1, max_gpus=1,
                priority=5, status=Job.Status.QUEUED,
            )
            for i in range(10)
        ]

        def loop():
            connection.close()
            for _ in range(4):
                engine.schedule_pool(pool.id, lock_timeout=30)

        errors = _run_in_threads([loop, loop, loop])
        self.assertEqual(errors, [])
        self.assertEqual(_active_count(pool.id), 3)
        self.assertEqual(
            Job.objects.filter(pool=pool, status=Job.Status.QUEUED).count(), 7
        )


class ConcurrentSettleRaceTests(TransactionTestCase, MySQLMixin):
    def setUp(self):
        self._skip_if_not_mysql()
        self.pool = ResourcePool.objects.create(name="settle-pool")
        self.node = Node.objects.create(
            pool=self.pool, name="s-node", gpu_count=8,
            gpu_memory_mb=1000, status=Node.Status.AVAILABLE,
        )
        self.node.last_heartbeat_at = timezone.now()
        self.node.save()
        self.job = Job.objects.create(
            pool=self.pool, name="s-job", min_gpus=8, max_gpus=8,
            priority=5, status=Job.Status.QUEUED,
        )
        engine.schedule_pool(self.pool.id)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.ALLOCATED)

    def test_complete_cancel_timeout_concurrent_settle_once(self):
        outcomes = []

        def complete():
            connection.close()
            outcomes.append(("complete",
                             reconcile.complete_job(self.job.id)))

        def cancel():
            connection.close()
            outcomes.append(("cancel", reconcile.cancel_job(self.job.id)))

        def timeout_settle():
            connection.close()
            outcomes.append((
                "timeout",
                reconcile.settle_job(
                    self.job.id, Job.Status.TIMED_OUT, reason="并发超时"
                ),
            ))

        errors = _run_in_threads([complete, cancel, timeout_settle])
        self.assertEqual(errors, [])

        # 恰好一方结算成功，另外两方幂等失败
        successes = [name for name, ok in outcomes if ok]
        self.assertEqual(len(successes), 1, outcomes)

        # 资源只释放一次：分配表为空，节点空闲
        self.assertEqual(Allocation.objects.filter(job=self.job).count(), 0)
        self.assertEqual(_total_used(self.pool.id), 0)
        self.node.refresh_from_db()
        self.assertEqual(self.node.status, Node.Status.AVAILABLE)

        self.job.refresh_from_db()
        self.assertIn(self.job.status, Job.TERMINAL_STATUSES)
        self.assertNotEqual(self.job.status, Job.Status.ALLOCATED)


class PreemptionFailureRecoveryTests(TransactionTestCase, MySQLMixin):
    def setUp(self):
        self._skip_if_not_mysql()
        self.pool = ResourcePool.objects.create(
            name="pre-fail-pool", max_gpus=8, max_running_jobs=10
        )
        self.node = Node.objects.create(
            pool=self.pool, name="pf-node", gpu_count=8, gpu_memory_mb=1000,
            status=Node.Status.AVAILABLE,
        )
        self.node.last_heartbeat_at = timezone.now()
        self.node.save()
        self.victim = Job.objects.create(
            pool=self.pool, name="victim", min_gpus=8, max_gpus=8,
            priority=3, status=Job.Status.QUEUED,
        )
        engine.schedule_pool(self.pool.id)
        self.victim.refresh_from_db()
        self.assertEqual(self.victim.status, Job.Status.ALLOCATED)

        self.preemptor = Job.objects.create(
            pool=self.pool, name="preemptor", min_gpus=8, max_gpus=8,
            priority=9, status=Job.Status.QUEUED,
        )

    def test_failed_preemption_then_retry_succeeds(self):
        def boom(pre, vics):
            raise engine.SchedulingError("模拟重新分配失败")

        engine.PREEMPTION_FAULTS.append(boom)
        try:
            result = engine.schedule_pool(self.pool.id, lock_timeout=30)
        finally:
            engine.PREEMPTION_FAULTS.remove(boom)

        self.victim.refresh_from_db()
        self.preemptor.refresh_from_db()
        self.assertEqual(self.victim.status, Job.Status.ALLOCATED)
        self.assertEqual(self.preemptor.status, Job.Status.QUEUED)
        self.assertEqual(_total_used(self.pool.id), 8)
        self.assertFalse(result.preemptions[0]["success"])

        # 恢复：不再注入故障，重试调度 -> 抢占成功
        result2 = engine.schedule_pool(self.pool.id, lock_timeout=30)
        self.victim.refresh_from_db()
        self.preemptor.refresh_from_db()
        self.assertEqual(self.victim.status, Job.Status.PREEMPTED)
        self.assertEqual(self.preemptor.status, Job.Status.ALLOCATED)
        self.assertEqual(_total_used(self.pool.id), 8)  # 无泄漏/超卖
        self.assertTrue(result2.preemptions[0]["success"])


class LostNodeRecoveryTests(TransactionTestCase, MySQLMixin):
    def setUp(self):
        self._skip_if_not_mysql()
        self.pool = ResourcePool.objects.create(name="lost-pool")
        self.node = Node.objects.create(
            pool=self.pool, name="lost-node", gpu_count=8, gpu_memory_mb=1000,
            status=Node.Status.AVAILABLE, last_heartbeat_at=timezone.now()
            - timedelta(seconds=120),  # 已超时
        )
        self.job = Job.objects.create(
            pool=self.pool, name="lost-job", min_gpus=8, max_gpus=8,
            priority=5, status=Job.Status.QUEUED,
        )
        # 手工制造“失联前已分配”的事实
        self.job.status = Job.Status.ALLOCATED
        self.job.allocated_at = timezone.now()
        self.job.save()
        Allocation.objects.create(job=self.job, node=self.node, gpu_count=8)

    def test_stale_node_concurrent_with_scheduler(self):
        observed = {}

        def detect():
            connection.close()
            observed["stale"] = reconcile.detect_stale_nodes()

        def schedule():
            connection.close()
            # 无论先后，失联节点的卡都不能被新作业使用
            new_job = Job.objects.create(
                pool=self.pool, name="after-lost", min_gpus=1, max_gpus=1,
                priority=9, status=Job.Status.QUEUED,
            )
            engine.schedule_pool(self.pool.id, lock_timeout=30)
            new_job.refresh_from_db()
            observed["new_job_status"] = new_job.status

        errors = _run_in_threads([detect, schedule])
        self.assertEqual(errors, [])
        self.assertEqual(observed["stale"], [self.node.id])
        self.assertEqual(observed["new_job_status"], Job.Status.QUEUED)

        self.node.refresh_from_db()
        self.job.refresh_from_db()
        self.assertEqual(self.node.status, Node.Status.OFFLINE)
        self.assertEqual(self.job.status, Job.Status.FAILED)
        self.assertEqual(Allocation.objects.count(), 0)

    def test_recovery_then_schedulable_again(self):
        reconcile.detect_stale_nodes()
        self.node.refresh_from_db()
        self.assertEqual(self.node.status, Node.Status.OFFLINE)

        # 节点重新心跳恢复，之后新作业可调度
        reconcile.heartbeat(self.node.id)
        new_job = Job.objects.create(
            pool=self.pool, name="recovered-job", min_gpus=2, max_gpus=2,
            priority=5, status=Job.Status.QUEUED,
        )
        engine.schedule_pool(self.pool.id)
        new_job.refresh_from_db()
        self.assertEqual(new_job.status, Job.Status.ALLOCATED)
        self.node.refresh_from_db()
        self.assertEqual(self.node.status, Node.Status.OCCUPIED)


class QueueTimeoutRaceTests(TransactionTestCase, MySQLMixin):
    def setUp(self):
        self._skip_if_not_mysql()
        self.pool = ResourcePool.objects.create(name="timeout-pool")
        self.node = Node.objects.create(
            pool=self.pool, name="to-node", gpu_count=0, gpu_memory_mb=1000,
            status=Node.Status.AVAILABLE,
            last_heartbeat_at=timezone.now() - timedelta(hours=3),
        )
        self.job = Job.objects.create(
            pool=self.pool, name="old-job", min_gpus=1, max_gpus=1,
            priority=5, status=Job.Status.QUEUED,
            queued_at=timezone.now() - timedelta(hours=3),
        )

    def test_timeout_and_skip_only_settles_once(self):
        states = []

        def sched_a():
            connection.close()
            r = engine.schedule_pool(self.pool.id, lock_timeout=30)
            states.append(("A", r.timed_out_queued if r else None))

        def sched_b():
            connection.close()
            r = engine.schedule_pool(self.pool.id, lock_timeout=30)
            states.append(("B", r.timed_out_queued if r else None))

        errors = _run_in_threads([sched_a, sched_b])
        self.assertEqual(errors, [])

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.TIMED_OUT)
        # 超时只记录一次
        from scheduler.models import SchedulingDecision

        self.assertEqual(
            SchedulingDecision.objects.filter(
                job=self.job, kind=SchedulingDecision.Kind.QUEUE_TIMEOUT
            ).count(),
            1,
        )
