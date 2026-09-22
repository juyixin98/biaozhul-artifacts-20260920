"""Node loss / heartbeat recovery, settlement idempotency and restart
recovery from the database."""
from scheduler import services
from scheduler.clock import FakeClock
from scheduler.models import (
    Allocation,
    DecisionKind,
    FinishReason,
    JobState,
)

from .base import SchedulerTestBase
from .test_scheduling import register


class NodeLivenessTests(SchedulerTestBase):
    def test_stale_heartbeat_marks_node_offline_and_fails_jobs(self):
        pool, node = self.make_node(gpus=4)
        a = self.submit(pool, "a", mn=2, mx=2)
        self.tick()
        self.assertEqual(self.refresh(a).state, JobState.RUNNING)

        # Lost contact: its GPUs must not be treated as available.
        self.advance(31)
        b = self.submit(pool, "b", mn=2, mx=2)
        stats = self.tick()
        self.assertEqual(stats["lost_nodes"], 1)
        self.refresh(node)
        self.assertTrue(node.marked_offline_at is not None)
        self.assertEqual(node.allocated_gpus, 0)
        self.assertEqual(self.refresh(a).state, JobState.FAILED)
        self.assertEqual(self.refresh(a).finish_reason, FinishReason.NODE_LOST)
        # b cannot land on the lost node.
        self.assertEqual(self.refresh(b).state, JobState.PENDING)

    def test_heartbeat_recovery_restores_node(self):
        pool, node = self.make_node(gpus=2)
        job = self.submit(pool, mn=1, mx=1)
        self.tick()

        self.advance(40)
        self.tick()  # declared lost
        self.refresh(node)
        self.assertTrue(node.marked_offline_at is not None)
        self.assertEqual(self.refresh(job).state, JobState.FAILED)

        # Agent comes back and resumes heartbeats: node accepts work again.
        services.heartbeat(node.hostname, self.now())
        node.refresh_from_db()
        self.assertTrue(node.marked_offline_at is None)
        self.assertTrue(
            pool.decisions.filter(kind=DecisionKind.NODE_RECOVERED).exists()
        )
        new_job = self.submit(pool, "new", mn=1, mx=1)
        self.tick()
        self.assertEqual(self.refresh(new_job).state, JobState.RUNNING)

    def test_admin_offline_settles_jobs_and_requires_reenable(self):
        pool, node = self.make_node(gpus=2)
        job = self.submit(pool, mn=1, mx=1)
        self.tick()
        services.set_admin_state(node, "OFFLINE", self.now())
        self.assertEqual(self.refresh(job).state, JobState.FAILED)
        self.assertEqual(self.node(node.hostname).allocated_gpus, 0)

        # A heartbeat while still administratively offline does NOT recover it.
        self.advance(5)
        services.heartbeat(node.hostname, self.now())
        self.assertTrue(self.node(node.hostname).marked_offline_at is not None)

        services.set_admin_state(node, "ONLINE", self.now())
        services.heartbeat(node.hostname, self.now())
        self.assertTrue(self.node(node.hostname).marked_offline_at is None)

    def test_lost_node_allocation_is_never_reused(self):
        # While the node is lost, repeated ticks create no allocations on it.
        pool, node = self.make_node(gpus=2)
        self.submit(pool, mn=1, mx=1)
        self.tick()
        self.advance(100)
        self.tick()
        for _ in range(3):
            self.submit(pool, "late", mn=1, mx=1)
            self.tick()
        self.assertEqual(
            Allocation.objects.filter(node=node).count(), 0
        )

    def test_no_spurious_preemption_targets_a_lost_node(self):
        # A registered-but-never-heartbeating node is OFFLINE. A high-priority
        # job that would "fit on paper" must neither be placed nor trigger an
        # empty preemption against that node.
        pool = self.make_pool("dead", max_gpus=8, max_jobs=8)
        services.register_node(pool, "ghost", 8, 81920)  # no heartbeat
        urgent = self.submit(pool, "urgent", mn=8, mx=8, prio=10)
        self.tick()
        self.assertEqual(self.refresh(urgent).state, JobState.PENDING)
        from scheduler.models import PreemptionRequest
        self.assertEqual(PreemptionRequest.objects.count(), 0)
        self.assertEqual(Allocation.objects.count(), 0)


class SettlementIdempotencyTests(SchedulerTestBase):
    def test_double_completion_settles_once(self):
        pool, _ = self.make_node(gpus=1)
        job = self.submit(pool, mn=1, mx=1)
        self.tick()
        w1, j1 = services.report_completion(job, self.now())
        w2, j2 = services.report_completion(job, self.now())
        self.assertTrue(w1)
        self.assertFalse(w2)  # second settler loses the CAS
        settle_rows = pool.decisions.filter(
            kind=DecisionKind.SETTLED, job=job
        )
        self.assertEqual(settle_rows.count(), 1)
        self.assertEqual(self.node("n1").allocated_gpus, 0)

    def test_completion_and_cancel_race_settles_once(self):
        pool, _ = self.make_node(gpus=1)
        job = self.submit(pool, mn=1, mx=1)
        self.tick()
        # Both "concurrent" operations are attempted; exactly one wins.
        r_complete = services.report_completion(job, self.now())
        r_cancel = services.cancel_job(job, self.now())
        outcomes = sorted([r_complete[0], r_cancel[0]])
        self.assertEqual(outcomes, [False, True])
        job.refresh_from_db()
        self.assertIn(
            (job.state, job.finish_reason),
            [
                (JobState.COMPLETED, FinishReason.COMPLETED),
                (JobState.CANCELLED, FinishReason.USER_CANCELLED),
            ],
        )
        self.assertEqual(self.node("n1").allocated_gpus, 0)
        self.assertEqual(
            pool.decisions.filter(kind=DecisionKind.SETTLED, job=job).count(), 1
        )

    def test_timeout_cannot_settle_a_running_job(self):
        pool, _ = self.make_node(gpus=1)
        job = self.submit(pool, mn=1, mx=1)
        self.tick()
        self.advance(3 * 3600)
        # Keep the node alive across the long jump (otherwise it'd correctly
        # be declared lost); even then, the queue-timeout phase must only
        # cancel PENDING jobs, not this RUNNING one.
        services.heartbeat("n1", self.now())
        self.tick()
        self.assertEqual(self.refresh(job).state, JobState.RUNNING)

    def test_cancel_queued_job_has_no_allocation_to_release(self):
        pool, _ = self.make_node(gpus=1)
        j1 = self.submit(pool, "j1", mn=1, mx=1)
        j2 = self.submit(pool, "j2", mn=1, mx=1)
        self.tick()
        won, _ = services.cancel_job(j2, self.now())
        self.assertTrue(won)
        j2.refresh_from_db()
        self.assertEqual(j2.state, JobState.CANCELLED)
        self.assertEqual(j2.finish_reason, FinishReason.USER_CANCELLED)
        self.assertEqual(Allocation.objects.count(), 1)


class RestartRecoveryTests(SchedulerTestBase):
    def test_allocations_rebuilt_from_database_after_restart(self):
        pool, node = self.make_node(gpus=4)
        jobs = [self.submit(pool, f"j{i}", mn=1, mx=1) for i in range(4)]
        self.tick()
        for j in jobs:
            self.assertEqual(self.refresh(j).state, JobState.RUNNING)

        # Simulate counter drift / a fresh process with no in-memory state.
        node.allocated_gpus = 0
        node.save(update_fields=["allocated_gpus"])
        rec = services.recover_from_database(self.now())
        self.assertEqual(rec["nodes_reconciled"], 1)
        self.assertEqual(self.node("n1").allocated_gpus, 4)

        # Node fully packed. Free one GPU (3 still in use): a min-2 job still
        # cannot fit — the single free slot stays unusable for it.
        services.report_completion(jobs[0], self.now())
        waiting = self.submit(pool, "waiting", mn=2, mx=2)
        self.tick()
        self.assertEqual(self.refresh(waiting).state, JobState.PENDING)
        self.assertEqual(self.node("n1").allocated_gpus, 3)

        # Free a second GPU -> 2 contiguous GPUs available -> placed.
        services.report_completion(jobs[1], self.now())
        self.tick()
        self.assertEqual(self.refresh(waiting).state, JobState.RUNNING)
        # 2 survivors (1 each) + the new 2-GPU job -> node fully packed again.
        self.assertEqual(self.node("n1").allocated_gpus, 4)
        self.assertEqual(Allocation.objects.get(job=waiting).gpu_count, 2)

    def test_pending_preemption_survives_restart(self):
        pool, node = self.make_node(gpus=2)
        v = self.submit(pool, "v", mn=2, mx=2, prio=1)
        self.tick()
        urgent = self.submit(pool, "u", mn=2, mx=2, prio=10)
        self.tick()  # PREEMPTING / request PENDING, persisted in MySQL.

        # New process rebuilds state and continues the same 2-phase flow.
        services.recover_from_database(self.now())
        services.report_preempted_release(v, self.now())
        self.tick()
        self.tick()
        self.assertEqual(self.refresh(urgent).state, JobState.RUNNING)
        self.assertEqual(self.node("n1").allocated_gpus, 2)
