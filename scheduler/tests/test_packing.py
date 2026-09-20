"""装箱算法与同优先级排序规则的单元测试（纯函数，SQLite/MySQL 均可）。"""
from datetime import timedelta

from django.test import TestCase

from scheduler.clock import FakeClock
from scheduler.packing import NodeCapacity, job_sort_key, plan_allocation


def cap(node_id, free, total=None, mem=1000, status="available"):
    return NodeCapacity(
        node_id=node_id, free_gpus=free,
        total_gpus=total if total is not None else free,
        gpu_memory_mb=mem, status=status,
    )


class PackingTests(TestCase):
    def test_best_fit_prefers_tighter_node(self):
        # 空闲 [2, 8]，需要 2：应放进最小能装下的 2 卡节点，保留 8 卡大块。
        result = plan_allocation(
            min_gpus=2, max_gpus=2, required_memory_mb=0,
            candidates=[cap(1, 8), cap(2, 2)],
        )
        self.assertTrue(result["feasible"])
        self.assertEqual(result["plan"], {2: 2})

    def test_spreads_to_collect_max_gpus(self):
        # 需要 1..10，空闲 3+4：应尽量凑到 max=7。
        result = plan_allocation(
            min_gpus=1, max_gpus=10, required_memory_mb=0,
            candidates=[cap(1, 3), cap(2, 4)],
        )
        self.assertEqual(result["allocated_gpus"], 7)
        self.assertEqual(result["plan"], {1: 3, 2: 4})

    def test_elastic_min_satisfied(self):
        result = plan_allocation(
            min_gpus=4, max_gpus=8, required_memory_mb=0,
            candidates=[cap(1, 3), cap(2, 3)],
        )
        self.assertTrue(result["feasible"])
        self.assertEqual(result["allocated_gpus"], 6)

    def test_insufficient_capacity(self):
        result = plan_allocation(
            min_gpus=5, max_gpus=8, required_memory_mb=0,
            candidates=[cap(1, 2), cap(2, 2)],
        )
        self.assertFalse(result["feasible"])
        self.assertIn("空闲 GPU 共 4", result["reason"])

    def test_memory_filter(self):
        result = plan_allocation(
            min_gpus=1, max_gpus=2, required_memory_mb=8000,
            candidates=[cap(1, 8, mem=4096), cap(2, 2, mem=16000)],
        )
        self.assertTrue(result["feasible"])
        self.assertEqual(result["plan"], {2: 2})

    def test_no_eligible_memory(self):
        result = plan_allocation(
            min_gpus=1, max_gpus=1, required_memory_mb=99999,
            candidates=[cap(1, 8, mem=4096)],
        )
        self.assertFalse(result["feasible"])
        self.assertIn("显存", result["reason"])

    def test_draining_and_offline_excluded_by_caller(self):
        # 非可调度节点不应由调用方传入；传空候选时给出明确原因。
        result = plan_allocation(
            min_gpus=1, max_gpus=1, required_memory_mb=0, candidates=[]
        )
        self.assertFalse(result["feasible"])
        self.assertIn("没有可调度节点", result["reason"])

    def test_deterministic_tie_break(self):
        r1 = plan_allocation(
            min_gpus=1, max_gpus=4, required_memory_mb=0,
            candidates=[cap(3, 4), cap(1, 4), cap(2, 4)],
        )
        r2 = plan_allocation(
            min_gpus=1, max_gpus=4, required_memory_mb=0,
            candidates=[cap(2, 4), cap(3, 4), cap(1, 4)],
        )
        self.assertEqual(r1["plan"], r2["plan"])
        self.assertEqual(r1["plan"], {1: 4})

    def test_job_sort_priority_then_fifo(self):
        from scheduler.tests.factories import make_job, make_pool

        pool = make_pool("sort-pool")
        clock = FakeClock()
        hi_old = make_job(pool, "hi-old", priority=9, clock=clock)
        clock.advance(10)
        low = make_job(pool, "low", priority=2, clock=clock)
        clock.advance(10)
        hi_new = make_job(pool, "hi-new", priority=9, clock=clock)

        ordered = sorted([low, hi_new, hi_old], key=job_sort_key)
        self.assertEqual(
            [j.name for j in ordered], ["hi-old", "hi-new", "low"]
        )
