"""
调度引擎功能测试（按顺序验证，SQLite 内存锁即可覆盖正确性；
真正的多线程/多连接并发在 test_concurrency_mysql.py 中验证）。
"""
from django.test import TransactionTestCase

from scheduler import engine, reconcile
from scheduler.clock import FakeClock
from scheduler.models import (
    Allocation,
    Job,
    Node,
    PreemptionRecord,
    SchedulingDecision,
)
from scheduler.tests.factories import (
    make_job,
    make_node,
    make_pool,
    node_used,
    used_gpus_of_pool,
)


class SchedulerBasicTests(TransactionTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.pool = make_pool("p", max_gpus=20, max_running_jobs=10)
        self.n1 = make_node(self.pool, "n1", gpus=8, mem=16000,
                            clock=self.clock)
        self.n2 = make_node(self.pool, "n2", gpus=4, mem=8000,
                            clock=self.clock)

    def test_schedule_allocates_and_records_rationale(self):
        job = make_job(self.pool, "j", min_gpus=2, max_gpus=4, mem=8000,
                       priority=5, clock=self.clock)
        result = engine.schedule_pool(self.pool.id, clock=self.clock)

        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.ALLOCATED)
        # best-fit：n2(4) 先被装满而不是 n1(8)
        self.assertEqual(node_used(self.n2), 4)
        self.assertIn(job.id, result.scheduled)
        decision = SchedulingDecision.objects.get(
            job=job, kind=SchedulingDecision.Kind.SCHEDULE
        )
        self.assertEqual(decision.rationale["allocated_gpus"], 4)
        self.assertTrue(decision.rationale["plan"])

    def test_priority_order_high_first(self):
        # 总容量 20 卡：两个 8 卡作业都能放下，验证高优先级先被服务。
        make_node(self.pool, "n3", gpus=8, mem=8000, clock=self.clock)
        low = make_job(self.pool, "low", min_gpus=8, max_gpus=8, priority=2,
                       clock=self.clock)
        self.clock.advance(1)
        high = make_job(self.pool, "high", min_gpus=8, max_gpus=8, priority=9,
                        clock=self.clock)
        result = engine.schedule_pool(self.pool.id, clock=self.clock)
        self.assertEqual(result.scheduled, [high.id, low.id])

    def test_insufficient_resources_stays_queued(self):
        job = make_job(self.pool, "big", min_gpus=100, max_gpus=100,
                       priority=5, clock=self.clock)
        result = engine.schedule_pool(self.pool.id, clock=self.clock)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertEqual(result.scheduled, [])
        self.assertTrue(result.skipped)
        self.assertEqual(Allocation.objects.count(), 0)

    def test_low_priority_cannot_preempt(self):
        # 占满资源的低优先级作业，再来一个 7 级作业：不能抢占。
        victim = make_job(self.pool, "v", min_gpus=8, max_gpus=8, priority=3,
                          clock=self.clock)
        engine.schedule_pool(self.pool.id, clock=self.clock)
        victim.refresh_from_db()

        other = make_job(self.pool, "other", min_gpus=8, max_gpus=8, priority=7,
                         clock=self.clock)
        result = engine.schedule_pool(self.pool.id, clock=self.clock)
        other.refresh_from_db()
        victim.refresh_from_db()
        self.assertEqual(other.status, Job.Status.QUEUED)
        self.assertEqual(victim.status, Job.Status.ALLOCATED)
        self.assertEqual(result.preemptions, [])

    def test_pool_gpu_quota_enforced(self):
        # 节点物理上有 12 卡，但池配额只有 6。
        pool = make_pool("quota", max_gpus=6, max_running_jobs=0)
        make_node(pool, "q1", gpus=8, mem=1000, clock=self.clock)
        job = make_job(pool, "j", min_gpus=8, max_gpus=8, clock=self.clock)
        result = engine.schedule_pool(pool.id, clock=self.clock)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertTrue(
            any("配额" in s["reason"] for s in result.skipped)
        )

    def test_pool_concurrency_limit_enforced(self):
        pool = make_pool("conc", max_gpus=0, max_running_jobs=1)
        make_node(pool, "c1", gpus=8, mem=1000, clock=self.clock)
        j1 = make_job(pool, "j1", min_gpus=1, max_gpus=1, clock=self.clock)
        j2 = make_job(pool, "j2", min_gpus=1, max_gpus=1, clock=self.clock)
        result = engine.schedule_pool(pool.id, clock=self.clock)
        self.assertEqual(set(result.scheduled), {j1.id})
        self.assertTrue(
            any("并发" in s["reason"] for s in result.skipped)
        )

    def test_draining_node_rejects_new_work(self):
        reconcile.mark_draining(self.n1.id, clock=self.clock)
        # n2 只有 4 卡，作业要 6 卡 -> 必须排队，即使排空的 n1 有空闲。
        job = make_job(self.pool, "j", min_gpus=6, max_gpus=6, clock=self.clock)
        result = engine.schedule_pool(self.pool.id, clock=self.clock)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertEqual(used_gpus_of_pool(self.pool), 0)

    def test_draining_node_keeps_running_load(self):
        job = make_job(self.pool, "j", min_gpus=3, max_gpus=3, clock=self.clock)
        engine.schedule_pool(self.pool.id, clock=self.clock)
        reconcile.mark_draining(self.n2.id, clock=self.clock)
        self.n2.refresh_from_db()
        self.assertEqual(self.n2.status, Node.Status.DRAINING)
        self.assertEqual(node_used(self.n2), 3)  # 存量作业不被驱逐


class FragmentationTests(TransactionTestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_memory_fragmentation_blocks_assignment(self):
        # 两块空闲 8G 卡，但作业要 16G 单卡；跨节点无法满足单卡显存要求。
        pool = make_pool("frag")
        make_node(pool, "f1", gpus=4, mem=8192, clock=self.clock)
        make_node(pool, "f2", gpus=4, mem=8192, clock=self.clock)
        job = make_job(pool, "bigmem", min_gpus=1, max_gpus=4, mem=16384,
                       clock=self.clock)
        result = engine.schedule_pool(pool.id, clock=self.clock)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertIn("显存", result.skipped[0]["reason"])

    def test_small_nodes_cannot_fit_elastic_min(self):
        pool = make_pool("tiny")
        # 每个节点只有 1 卡，作业要求最少 2 卡但允许跨节点 -> 可满足。
        make_node(pool, "t1", gpus=1, mem=1000, clock=self.clock)
        make_node(pool, "t2", gpus=1, mem=1000, clock=self.clock)
        job = make_job(pool, "span", min_gpus=2, max_gpus=4, clock=self.clock)
        engine.schedule_pool(pool.id, clock=self.clock)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.ALLOCATED)
        self.assertEqual(used_gpus_of_pool(pool), 2)


class PreemptionTests(TransactionTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.pool = make_pool("pre", max_gpus=12, max_running_jobs=10)
        self.n1 = make_node(self.pool, "n1", gpus=8, mem=16000,
                            clock=self.clock)

    def _run_low_priority_job(self, priority=3, gpus=8):
        victim = make_job(
            self.pool, f"low-{priority}", min_gpus=gpus, max_gpus=gpus,
            priority=priority, clock=self.clock,
        )
        engine.schedule_pool(self.pool.id, clock=self.clock)
        victim.refresh_from_db()
        return victim

    def test_high_priority_preempts_low(self):
        victim = self._run_low_priority_job(priority=3, gpus=8)
        preemptor = make_job(
            self.pool, "hi", min_gpus=8, max_gpus=8, priority=9,
            clock=self.clock,
        )
        result = engine.schedule_pool(self.pool.id, clock=self.clock)

        preemptor.refresh_from_db()
        victim.refresh_from_db()
        self.assertEqual(preemptor.status, Job.Status.ALLOCATED)
        self.assertIn("抢占", preemptor.status_reason)
        self.assertEqual(victim.status, Job.Status.PREEMPTED)
        # 资源先释放再重新分配：同一时刻占用不超过物理容量
        self.assertEqual(used_gpus_of_pool(self.pool), 8)
        self.assertEqual(node_used(self.n1), 8)
        self.assertTrue(result.preemptions[0]["success"])
        rec = PreemptionRecord.objects.get(preemptor=preemptor)
        self.assertTrue(rec.success)

    def test_priority_8_can_preempt_3(self):
        self._run_low_priority_job(priority=3, gpus=4)
        p = make_job(self.pool, "p8", min_gpus=4, max_gpus=8, priority=8,
                     clock=self.clock)
        engine.schedule_pool(self.pool.id, clock=self.clock)
        p.refresh_from_db()
        self.assertEqual(p.status, Job.Status.ALLOCATED)

    def test_cannot_preempt_priority_4(self):
        victim = self._run_low_priority_job(priority=4, gpus=8)
        p = make_job(self.pool, "hi", min_gpus=8, max_gpus=8, priority=10,
                     clock=self.clock)
        result = engine.schedule_pool(self.pool.id, clock=self.clock)
        p.refresh_from_db()
        victim.refresh_from_db()
        self.assertEqual(p.status, Job.Status.QUEUED)
        self.assertEqual(victim.status, Job.Status.ALLOCATED)
        self.assertEqual(result.preemptions, [])

    def test_preemption_failure_rolls_back(self):
        """注入故障：受害者已释放，但重新分配阶段失败 -> savepoint 回滚，
        受害者复活、资源不泄漏。"""
        victim = self._run_low_priority_job(priority=3, gpus=8)
        preemptor = make_job(
            self.pool, "hi-fail", min_gpus=8, max_gpus=8, priority=9,
            clock=self.clock,
        )

        def boom(pre, vics):
            raise engine.SchedulingError("注入的分配阶段故障")

        engine.PREEMPTION_FAULTS.append(boom)
        try:
            result = engine.schedule_pool(self.pool.id, clock=self.clock)
        finally:
            engine.PREEMPTION_FAULTS.remove(boom)

        preemptor.refresh_from_db()
        victim.refresh_from_db()
        self.assertEqual(preemptor.status, Job.Status.QUEUED)
        self.assertEqual(victim.status, Job.Status.ALLOCATED)  # 回滚复活
        self.assertEqual(node_used(self.n1), 8)  # 资源未泄漏
        self.assertFalse(result.preemptions[0]["success"])
        self.assertIn("注入", result.preemptions[0]["reason"])
        failed = PreemptionRecord.objects.get(victim=victim)
        self.assertFalse(failed.success)

    def test_preemption_chooses_weakest_then_fewest_gpus(self):
        # 两个可抢占作业：p1 占 2 卡、p3 占 6 卡；只需要 2 卡时应抢 p1。
        pool = make_pool("multi", max_gpus=8, max_running_jobs=10)
        node = make_node(pool, "m1", gpus=8, mem=1000, clock=self.clock)
        j6 = make_job(pool, "j6", min_gpus=6, max_gpus=6, priority=3,
                      clock=self.clock)
        j2 = make_job(pool, "j2", min_gpus=2, max_gpus=2, priority=1,
                      clock=self.clock)
        engine.schedule_pool(pool.id, clock=self.clock)
        # 再塞满：新抢占者需要 2 卡（节点已占 8）
        hp = make_job(pool, "hp", min_gpus=2, max_gpus=2, priority=9,
                      clock=self.clock)
        result = engine.schedule_pool(pool.id, clock=self.clock)
        hp.refresh_from_db()
        j2.refresh_from_db()
        j6.refresh_from_db()
        self.assertEqual(hp.status, Job.Status.ALLOCATED)
        self.assertEqual(j2.status, Job.Status.PREEMPTED)  # 最弱最少被抢
        self.assertEqual(j6.status, Job.Status.ALLOCATED)


class SettleIdempotencyTests(TransactionTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.pool = make_pool("p")
        self.node = make_node(self.pool, "n1", gpus=4, clock=self.clock)
        self.job = make_job(self.pool, "j", min_gpus=4, max_gpus=4,
                            clock=self.clock)
        engine.schedule_pool(self.pool.id, clock=self.clock)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.ALLOCATED)

    def test_complete_releases_once(self):
        self.assertTrue(reconcile.complete_job(self.job.id, clock=self.clock))
        self.assertFalse(reconcile.complete_job(self.job.id, clock=self.clock))
        self.assertFalse(reconcile.cancel_job(self.job.id, clock=self.clock))
        self.assertEqual(Allocation.objects.count(), 0)
        self.node.refresh_from_db()
        self.assertEqual(self.node.status, Node.Status.AVAILABLE)
        # 只产生一条 SETTLE 决策
        self.assertEqual(
            SchedulingDecision.objects.filter(
                kind=SchedulingDecision.Kind.SETTLE
            ).count(),
            1,
        )

    def test_cancel_then_complete_is_noop(self):
        self.assertTrue(reconcile.cancel_job(self.job.id, clock=self.clock))
        self.assertFalse(reconcile.complete_job(self.job.id, clock=self.clock))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.CANCELLED)
        self.assertEqual(Allocation.objects.count(), 0)


class TimeoutTests(TransactionTestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_queued_over_two_hours_cancelled(self):
        pool = make_pool("t")
        make_node(pool, "n", gpus=0, clock=self.clock)
        # 节点 0 卡，作业永远分不到资源；从当前时刻起排队。
        job = make_job(pool, "wait", min_gpus=1, max_gpus=1,
                       clock=self.clock)
        self.clock.advance(2 * 3600 + 1)
        result = engine.schedule_pool(pool.id, clock=self.clock)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.TIMED_OUT)
        self.assertIn(job.id, result.timed_out_queued)
        self.assertIn("2 小时", job.status_reason)

    def test_queued_within_two_hours_stays_queued(self):
        pool = make_pool("t2")
        make_node(pool, "n", gpus=0, clock=self.clock)
        job = make_job(pool, "wait", min_gpus=1, max_gpus=1,
                       clock=self.clock)
        self.clock.advance(2 * 3600 - 1)
        engine.schedule_pool(pool.id, clock=self.clock)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)

    def test_running_timeout_settles(self):
        pool = make_pool("rt")
        make_node(pool, "n", gpus=2, clock=self.clock)
        job = make_job(pool, "run", min_gpus=2, max_gpus=2, priority=5,
                       clock=self.clock, run_timeout_seconds=100)
        engine.schedule_pool(pool.id, clock=self.clock)
        job.refresh_from_db()
        job.status = Job.Status.RUNNING
        job.started_at = self.clock.now()
        job.save()
        self.clock.advance(101)
        ids = reconcile.timeout_expired_runs(clock=self.clock)
        self.assertEqual(ids, [job.id])
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.TIMED_OUT)
        self.assertEqual(Allocation.objects.count(), 0)
        # 幂等：再次扫描无效果
        self.assertEqual(reconcile.timeout_expired_runs(clock=self.clock), [])


class NodeLifecycleTests(TransactionTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.pool = make_pool("p", max_gpus=10)
        self.node = make_node(self.pool, "n1", gpus=4,
                              heartbeat_ago_seconds=0, clock=self.clock)

    def test_lost_node_not_schedulable_and_jobs_failed(self):
        job = make_job(self.pool, "j", min_gpus=4, max_gpus=4,
                       clock=self.clock)
        engine.schedule_pool(self.pool.id, clock=self.clock)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.ALLOCATED)

        # 心跳超时
        self.clock.advance(31)
        stale = reconcile.detect_stale_nodes(clock=self.clock)
        self.assertEqual(stale, [self.node.id])
        self.node.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(self.node.status, Node.Status.OFFLINE)
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertEqual(Allocation.objects.count(), 0)
        self.assertIn("失联", job.status_reason)

        # 失联节点上的资源不会被当作可用：新作业继续排队
        other = make_job(self.pool, "other", min_gpus=1, max_gpus=1,
                         clock=self.clock)
        result = engine.schedule_pool(self.pool.id, clock=self.clock)
        other.refresh_from_db()
        self.assertEqual(other.status, Job.Status.QUEUED)
        self.assertTrue(result.skipped)

    def test_node_recovers_on_heartbeat(self):
        self.clock.advance(31)
        reconcile.detect_stale_nodes(clock=self.clock)
        self.node.refresh_from_db()
        self.assertEqual(self.node.status, Node.Status.OFFLINE)

        reconcile.heartbeat(self.node.id, clock=self.clock)
        self.node.refresh_from_db()
        self.assertEqual(self.node.status, Node.Status.AVAILABLE)

        job = make_job(self.pool, "j", min_gpus=2, max_gpus=2,
                       clock=self.clock)
        engine.schedule_pool(self.pool.id, clock=self.clock)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.ALLOCATED)

    def test_drain_then_activate(self):
        reconcile.mark_draining(self.node.id, clock=self.clock)
        self.node.refresh_from_db()
        self.assertEqual(self.node.status, Node.Status.DRAINING)
        reconcile.activate_node(self.node.id, clock=self.clock)
        self.node.refresh_from_db()
        self.assertEqual(self.node.status, Node.Status.AVAILABLE)


class RestartRecoveryTests(TransactionTestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_allocation_restored_from_db(self):
        pool = make_pool("p")
        n1 = make_node(pool, "n1", gpus=4, clock=self.clock)
        n2 = make_node(pool, "n2", gpus=4, clock=self.clock)
        j1 = make_job(pool, "j1", min_gpus=2, max_gpus=2, clock=self.clock)
        j2 = make_job(pool, "j2", min_gpus=4, max_gpus=4, clock=self.clock)
        engine.schedule_pool(pool.id, clock=self.clock)

        # j1(2) -> n1；j2(4) 跨 n1(2)+n2(2)，共 3 条分配明细。
        alloc_rows = list(
            Allocation.objects.values_list("job_id", "node_id", "gpu_count")
        )
        self.assertEqual(len(alloc_rows), 3)

        # 模拟重启：不依赖任何内存计数，只从 Allocation 对账。
        Node.objects.update(status=Node.Status.AVAILABLE)
        summary = reconcile.recover_on_startup(clock=self.clock)
        self.assertEqual(summary["active_jobs"], 2)
        self.assertEqual(summary["allocations"], 3)
        self.assertEqual(summary["nodes_corrected"], 2)
        n1.refresh_from_db()
        n2.refresh_from_db()
        self.assertEqual(n1.status, Node.Status.OCCUPIED)
        self.assertEqual(n2.status, Node.Status.OCCUPIED)

        # 恢复后资源占用仍被正确计入，新作业无法超卖
        j3 = make_job(pool, "j3", min_gpus=4, max_gpus=4, clock=self.clock)
        result = engine.schedule_pool(pool.id, clock=self.clock)
        j3.refresh_from_db()
        self.assertEqual(j3.status, Job.Status.QUEUED)
        self.assertTrue(result.skipped)
