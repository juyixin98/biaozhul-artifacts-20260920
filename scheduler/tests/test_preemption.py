"""Preemption tests: threshold rule, release-before-reallocate ordering,
grace timeout, node loss failure, victim completion race and recovery."""
from scheduler import services
from scheduler.models import (
    Allocation,
    DecisionKind,
    FinishReason,
    Job,
    JobState,
    PreemptionRequest,
    PreemptionStatus,
)

from .base import SchedulerTestBase, override_scheduler
from .test_scheduling import register


def alloc_of(job):
    return Allocation.objects.filter(job=job).first()


class PreemptionRuleTests(SchedulerTestBase):
    def setUp(self):
        super().setUp()
        self.pool = self.make_pool("p", max_gpus=8, max_jobs=8)
        register(self, self.pool, "n1", 4)

    def _fill_node(self, *prios, gpus=1):
        jobs = []
        for i, p in enumerate(prios):
            jobs.append(self.submit(self.pool, f"v{i}", mn=gpus, mx=gpus, prio=p))
        self.tick()
        return jobs

    def test_priority_8_can_preempt_priority_3(self):
        victims = self._fill_node(3, 3, 3, 3)
        urgent = self.submit(self.pool, "urgent", mn=4, mx=4, prio=8)

        stats = self.tick()
        self.assertEqual(stats["preemptions_started"], 1)
        self.assertEqual(stats["placements"], 0)  # nothing reallocated yet

        req = PreemptionRequest.objects.get(beneficiary=urgent)
        self.assertEqual(req.status, PreemptionStatus.PENDING)
        self.assertEqual(req.target_node.hostname, "n1")
        self.assertEqual(req.victim_rows.count(), 4)
        for v in victims:
            self.assertEqual(self.refresh(v).state, JobState.PREEMPTING)
        self.assertEqual(self.refresh(urgent).state, JobState.PENDING)

        # Audit contains the full basis.
        d = self.pool.decisions.get(kind=DecisionKind.PREEMPT_REQUESTED)
        self.assertEqual(len(d.detail["victims"]), 4)
        self.assertEqual(d.detail["rule"], "priority>=8 may preempt priority<=3")

    def test_priority_7_cannot_preempt(self):
        self._fill_node(3, 3, 3, 3)
        job = self.submit(self.pool, "p7", mn=4, mx=4, prio=7)
        self.tick()
        self.assertEqual(PreemptionRequest.objects.count(), 0)
        self.assertEqual(self.refresh(job).state, JobState.PENDING)

    def test_priority_8_cannot_preempt_priority_4(self):
        self._fill_node(4, 4, 4, 4)
        job = self.submit(self.pool, "p8", mn=4, mx=4, prio=8)
        self.tick()
        self.assertEqual(PreemptionRequest.objects.count(), 0)
        self.assertEqual(self.refresh(job).state, JobState.PENDING)

    def test_minimum_victims_chosen(self):
        # n1 with a 3-GPU p1 job and a 1-GPU p2 job; urgent needs 3 GPUs.
        big_victim = self.submit(self.pool, "bigv", mn=3, mx=3, prio=1)
        self.tick()
        small_victim = self.submit(self.pool, "smallv", mn=1, mx=1, prio=2)
        self.tick()
        self.assertEqual(alloc_of(big_victim).node.hostname, "n1")
        self.assertEqual(alloc_of(small_victim).node.hostname, "n1")

        urgent = self.submit(self.pool, "urgent", mn=3, mx=3, prio=10)
        self.tick()
        req = PreemptionRequest.objects.get(beneficiary=urgent)
        self.assertEqual(list(req.victim_rows.values_list("job_id", flat=True)),
                         [big_victim.id])  # one big victim beats three small


class TwoPhaseReleaseTests(SchedulerTestBase):
    def setUp(self):
        super().setUp()
        self.pool = self.make_pool("p", max_gpus=8, max_jobs=8)
        register(self, self.pool, "n1", 2)

    def test_beneficiary_runs_only_after_victims_release(self):
        v1 = self.submit(self.pool, "v1", mn=1, mx=1, prio=2)
        v2 = self.submit(self.pool, "v2", mn=1, mx=1, prio=2)
        self.tick()
        urgent = self.submit(self.pool, "urgent", mn=2, mx=2, prio=9)

        self.tick()  # start preemption
        req = PreemptionRequest.objects.get(beneficiary=urgent)
        self.assertEqual(req.status, PreemptionStatus.PENDING)

        # Victims haven't released: repeated ticks must NOT place urgent and
        # must NOT oversell the node.
        for _ in range(3):
            self.tick()
        self.assertEqual(self.refresh(urgent).state, JobState.PENDING)
        self.assertEqual(Allocation.objects.filter(node__hostname="n1").count(), 2)

        # Worker for v1 releases; still one victim holding 1 GPU -> waiting.
        services.report_preempted_release(v1, self.now())
        self.tick()
        req.refresh_from_db()
        self.assertEqual(req.status, PreemptionStatus.PENDING)
        self.assertEqual(self.refresh(urgent).state, JobState.PENDING)

        # Second worker releases -> request RELEASED on this tick, and phase
        # D runs after phase C within the very same tick, so the beneficiary
        # is placed immediately — but only *after* every victim released.
        services.report_preempted_release(v2, self.now())
        stats = self.tick()
        self.assertEqual(stats["preemptions_released"], 1)
        self.assertEqual(stats["placements"], 1)
        req.refresh_from_db()
        self.assertEqual(req.status, PreemptionStatus.RELEASED)
        self.assertEqual(self.refresh(urgent).state, JobState.RUNNING)
        alloc = alloc_of(urgent)
        self.assertEqual(alloc.node.hostname, "n1")
        self.assertEqual(alloc.gpu_count, 2)
        self.assertEqual(self.node("n1").allocated_gpus, 2)

        # Victims settled exactly once with PREEMPTED reason.
        for v in (v1, v2):
            v.refresh_from_db()
            self.assertEqual(v.state, JobState.CANCELLED)
            self.assertEqual(v.finish_reason, FinishReason.PREEMPTED)

    def test_force_settle_after_grace_window(self):
        v = self.submit(self.pool, "v", mn=2, mx=2, prio=1)
        self.tick()
        urgent = self.submit(self.pool, "urgent", mn=2, mx=2, prio=10)
        self.tick()  # requested
        self.tick()  # still within 30s grace -> no force
        self.assertEqual(self.refresh(v).state, JobState.PREEMPTING)
        self.assertEqual(Allocation.objects.filter(job=v).count(), 1)

        # Keep the node alive while the 30s preemption grace elapses (the
        # heartbeat timeout is widened so liveness does not interfere).
        with override_scheduler(
            HEARTBEAT_TIMEOUT_SECONDS=10_000,
        ):
            self.advance(31)
            services.heartbeat("n1", self.now())
            stats = self.tick()  # grace elapsed -> force settle
        self.assertEqual(stats["forced_preemptions"], 1)
        self.assertEqual(self.refresh(v).finish_reason, FinishReason.PREEMPTED)
        self.assertFalse(Allocation.objects.filter(job=v).exists())

        self.tick()  # (idempotent no-op now: release+placement happened above)
        self.assertEqual(self.refresh(urgent).state, JobState.RUNNING)
        # Force-settle freed the 2 GPUs and the beneficiary took exactly 2.
        self.assertEqual(self.node("n1").allocated_gpus, 2)
        self.assertEqual(
            Allocation.objects.get(job=urgent).gpu_count, 2
        )

    def test_victim_completing_race_wins_as_completed(self):
        # Victim manages to finish normally at the same moment it is being
        # evicted. Either outcome is a single settlement; GPUs return once.
        v = self.submit(self.pool, "v", mn=2, mx=2, prio=2)
        self.tick()
        urgent = self.submit(self.pool, "urgent", mn=2, mx=2, prio=9)
        self.tick()  # v -> PREEMPTING
        self.assertEqual(self.refresh(v).state, JobState.PREEMPTING)

        won = services.report_completion(v, self.now())[0]
        self.assertTrue(won)
        self.refresh(v)
        self.assertEqual(v.state, JobState.COMPLETED)
        self.assertEqual(v.finish_reason, FinishReason.COMPLETED)
        self.assertEqual(self.node("n1").allocated_gpus, 0)

        # Scheduler now sees the allocation gone and releases the preemption.
        self.tick()
        req = PreemptionRequest.objects.get(beneficiary=urgent)
        self.assertEqual(req.status, PreemptionStatus.RELEASED)
        self.tick()
        self.assertEqual(self.refresh(urgent).state, JobState.RUNNING)


class PreemptionFailureTests(SchedulerTestBase):
    def setUp(self):
        super().setUp()
        self.pool = self.make_pool("p", max_gpus=8, max_jobs=8)
        register(self, self.pool, "n1", 2)

    def test_preemption_fails_when_target_node_lost(self):
        v = self.submit(self.pool, "v", mn=2, mx=2, prio=1)
        self.tick()
        urgent = self.submit(self.pool, "urgent", mn=2, mx=2, prio=10)
        self.tick()  # preemption requested, victim PREEMPTING

        # n1 loses contact while victims are still holding GPUs.
        self.advance(40)  # heartbeat timeout (30s)
        stats = self.tick()
        self.assertEqual(stats["lost_nodes"], 1)

        v.refresh_from_db()
        self.assertEqual(v.state, JobState.FAILED)
        self.assertEqual(v.finish_reason, FinishReason.NODE_LOST)
        req = PreemptionRequest.objects.get(beneficiary=urgent)
        self.assertEqual(req.status, PreemptionStatus.FAILED)
        self.assertEqual(req.fail_reason, "target_node_lost")
        self.assertTrue(
            self.pool.decisions.filter(
                kind=DecisionKind.PREEMPT_FAILED
            ).exists()
        )
        # Urgent job is NOT failed by the lost node (it never ran there); it
        # stays PENDING and recovers when capacity appears elsewhere.
        register(self, self.pool, "n2", 2)
        self.advance(1)  # distinct, later tick
        self.tick()
        self.assertEqual(self.refresh(urgent).state, JobState.RUNNING)
        self.assertEqual(
            Allocation.objects.get(job=urgent).node.hostname, "n2"
        )

    def test_beneficiary_cancelled_while_waiting_still_releases_victims(self):
        v = self.submit(self.pool, "v", mn=2, mx=2, prio=1)
        self.tick()
        urgent = self.submit(self.pool, "urgent", mn=2, mx=2, prio=10)
        self.tick()

        services.cancel_job(urgent, self.now())
        # Victim worker eventually acknowledges; request closes as failed,
        # but the node capacity is fully reclaimed.
        services.report_preempted_release(v, self.now())
        self.tick()
        req = PreemptionRequest.objects.get(beneficiary=urgent)
        self.assertEqual(req.status, PreemptionStatus.FAILED)
        self.assertEqual(req.fail_reason, "beneficiary_not_pending")
        self.assertEqual(self.node("n1").allocated_gpus, 0)


class QueueTimeoutTests(SchedulerTestBase):
    def test_pending_over_two_hours_is_cancelled(self):
        pool, _ = self.make_node(gpus=1)
        job = self.submit(pool, mn=2, mx=2)  # never fits
        self.tick()
        self.assertEqual(self.refresh(job).state, JobState.PENDING)

        self.advance(2 * 60 * 60)  # exactly at limit -> still waiting
        self.tick()
        self.assertEqual(self.refresh(job).state, JobState.PENDING)

        self.advance(1)
        stats = self.tick()
        self.assertEqual(stats["timeouts"], 1)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.CANCELLED)
        self.assertEqual(job.finish_reason, FinishReason.TIMEOUT)
