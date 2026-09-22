"""Placement, ordering, bin-packing/fragmentation and quota tests."""
from scheduler import services
from scheduler.models import (
    Allocation,
    Job,
    JobState,
    NodeState,
)

from .base import SchedulerTestBase


def register(testcase, pool, hostname, gpus, mem=81920, hb=True):
    node = services.register_node(pool, hostname, gpus, mem)
    if hb:
        services.heartbeat(hostname, testcase.now())
    return node


def alloc_node(job):
    return Allocation.objects.get(job=job).node.hostname


class BasicPlacementTests(SchedulerTestBase):
    def test_job_placed_on_heartbeating_node(self):
        pool, node = self.make_node(gpus=4)
        job = self.submit(pool, mn=1, mx=2)

        stats = self.tick()
        self.assertEqual(stats["placements"], 1)

        self.refresh(job)
        self.assertEqual(job.state, JobState.RUNNING)
        alloc = Allocation.objects.get(job=job)
        self.assertEqual(alloc.node_id, node.id)
        self.assertEqual(alloc.gpu_count, 2)  # takes up to max
        self.refresh(node)
        self.assertEqual(node.allocated_gpus, 2)
        self.assertEqual(node.effective_state(self.now(), 30), NodeState.OCCUPIED)

    def test_node_without_heartbeat_never_receives_work(self):
        pool = self.make_pool()
        node = services.register_node(pool, "ghost", 8, 81920)  # no heartbeat
        job = self.submit(pool)
        self.tick()
        self.refresh(job)
        self.assertEqual(job.state, JobState.PENDING)
        self.assertEqual(Allocation.objects.count(), 0)
        self.assertEqual(node.effective_state(self.now(), 30), NodeState.OFFLINE)

    def test_draining_node_gets_no_new_jobs_but_keeps_running(self):
        pool, node = self.make_node(gpus=8)
        job = self.submit(pool, mn=1, mx=1)
        self.tick()
        self.assertEqual(self.refresh(job).state, JobState.RUNNING)

        services.set_admin_state(node, "DRAINING", self.now())
        extra = self.submit(pool, "extra", mn=1)
        self.tick()
        self.assertEqual(self.refresh(job).state, JobState.RUNNING)  # untouched
        self.assertEqual(self.refresh(extra).state, JobState.PENDING)

    def test_memory_requirement_filters_nodes(self):
        pool = self.make_pool(max_gpus=16)
        register(self, pool, "small", 8, mem=16384)
        register(self, pool, "big", 8, mem=81920)
        job = self.submit(pool, mn=1, mx=1, mem=40960)  # 40GB cards only
        self.tick()
        self.assertEqual(alloc_node(job), "big")

    def test_min_gpus_larger_than_any_node_waits(self):
        self.make_node(gpus=4)
        job = self.submit(self.pool(), mn=8, mx=8)
        self.tick()
        self.assertEqual(self.refresh(job).state, JobState.PENDING)

    def test_unknown_node_heartbeat_is_rejected(self):
        # An agent that never registered must not be able to fake presence.
        with self.assertRaises(services.SchedulerError):
            services.heartbeat("impostor", self.now())


class OrderingTests(SchedulerTestBase):
    def test_priority_desc_jumps_queue_but_fifo_within_priority(self):
        pool = self.make_pool(max_gpus=16, max_jobs=8)
        register(self, pool, "n1", 1)
        register(self, pool, "n2", 1)

        low1 = self.submit(pool, "low1", prio=1)
        self.advance(10)
        high = self.submit(pool, "high", prio=10)
        self.advance(10)
        low2 = self.submit(pool, "low2", prio=1)

        self.tick()
        running = set(Allocation.objects.values_list("job__name", flat=True))
        self.assertEqual(running, {"low1", "high"})
        self.assertEqual(self.refresh(low2).state, JobState.PENDING)

    def test_same_priority_is_fifo(self):
        pool = self.make_pool()
        register(self, pool, "n1", 1)
        a = self.submit(pool, "a", prio=5)
        self.advance(5)
        b = self.submit(pool, "b", prio=5)
        self.tick()
        self.assertEqual(self.refresh(a).state, JobState.RUNNING)
        self.assertEqual(self.refresh(b).state, JobState.PENDING)


class BinPackingAndFragmentationTests(SchedulerTestBase):
    def test_best_fit_prefers_tightest_fit(self):
        # n1 has 8 GPUs, n2 has 4. A 2-GPU job fits both, but best-fit picks
        # n2 (residual 2) so the large node stays contiguous for big jobs.
        pool = self.make_pool(max_gpus=20)
        register(self, pool, "n1", 8)
        register(self, pool, "n2", 4)
        j2 = self.submit(pool, "two", mn=2, mx=2, prio=8)
        self.tick()
        self.assertEqual(alloc_node(j2), "n2")

        # n2 now free=2: a second 2-GPU job exhausts n2 (residual 0).
        j3 = self.submit(pool, "two-b", mn=2, mx=2, prio=7)
        self.tick()
        self.assertEqual(alloc_node(j3), "n2")

        # The 4-GPU job needs contiguity and finds it only on n1.
        j4 = self.submit(pool, "four", mn=4, mx=4, prio=6)
        self.tick()
        self.assertEqual(alloc_node(j4), "n1")

    def test_aggregate_free_is_not_usable_when_no_node_fits(self):
        # Construct fragmentation explicitly: each 2-GPU node ends with
        # exactly 1 GPU free, so a min-2 job cannot run despite 2 aggregate
        # free GPUs. (Direct allocation construction = pre-existing state.)
        from scheduler.models import Allocation

        pool = self.make_pool(max_gpus=8)
        n1 = register(self, pool, "n1", 2)
        n2 = register(self, pool, "n2", 2)

        f1 = self.submit(pool, "f1", mn=1, mx=1, prio=9)
        f2 = self.submit(pool, "f2", mn=1, mx=1, prio=9)
        self.tick()
        # Best-fit packs both fillers onto n1 first (residual 0 beats 1),
        # so move one allocation to n2 to create the fragmented shape.
        alloc2 = Allocation.objects.get(job=f2)
        alloc2.node = n2
        alloc2.save()
        n1.allocated_gpus, n2.allocated_gpus = 1, 1
        n1.save(update_fields=["allocated_gpus"])
        n2.save(update_fields=["allocated_gpus"])
        self.assertEqual(
            Allocation.objects.get(job=f1).node_id, n1.id
        )

        frag = self.submit(pool, "need-two", mn=2, mx=2, prio=5)
        self.tick()
        self.assertEqual(self.refresh(frag).state, JobState.PENDING)

    def test_completion_frees_gpus_for_waiting_job(self):
        pool, _ = self.make_node(hostname="n1", gpus=1)
        a = self.submit(pool, "a", mn=1, mx=1)
        self.tick()
        b = self.submit(pool, "b", mn=1, mx=1)
        self.tick()
        self.assertEqual(self.refresh(b).state, JobState.PENDING)

        won, _ = services.report_completion(a, self.now())
        self.assertTrue(won)
        self.tick()
        self.assertEqual(self.refresh(b).state, JobState.RUNNING)
        self.assertEqual(self.node("n1").allocated_gpus, 1)


class QuotaTests(SchedulerTestBase):
    def test_pool_concurrent_job_quota(self):
        pool = self.make_pool(max_gpus=16, max_jobs=2)
        register(self, pool, "n1", 8)
        register(self, pool, "n2", 8)
        for i in range(4):
            self.submit(pool, f"j{i}", mn=1, mx=1)
        self.tick()
        self.assertEqual(Allocation.objects.count(), 2)
        self.assertEqual(Job.objects.filter(state=JobState.PENDING).count(), 2)
        self.assertTrue(
            pool.decisions.filter(detail__reason="pool_concurrent_quota").exists()
        )

    def test_pool_gpu_quota_blocks_node_registration(self):
        pool = self.make_pool(max_gpus=4)
        register(self, pool, "n1", 4)
        with self.assertRaises(services.SchedulerError):
            register(self, pool, "n2", 1)

    def test_cannot_shrink_node_below_live_allocation(self):
        pool, node = self.make_node(hostname="n1", gpus=4)
        job = self.submit(pool, mn=2, mx=2)
        self.tick()
        with self.assertRaises(services.SchedulerError):
            services.register_node(pool, "n1", 1, 81920)

    def test_pool_quota_shrink_below_usage_rejected(self):
        pool, _ = self.make_node(gpus=4)
        self.submit(pool, mn=2, mx=2)
        self.tick()
        with self.assertRaises(services.SchedulerError):
            services.update_pool(pool, max_total_gpus=1)
