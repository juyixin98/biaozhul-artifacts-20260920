"""End-to-end REST API tests."""
from scheduler.models import (
    Allocation,
    FinishReason,
    JobState,
    NodeState,
)

from .base import SchedulerTestBase


class APIFlowTests(SchedulerTestBase):
    def _create_pool(self, name="demo", gpus=8, jobs=4):
        r = self.client.post(
            "/api/pools/",
            {
                "name": name,
                "max_total_gpus": gpus,
                "max_concurrent_jobs": jobs,
                "description": "d",
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["id"]

    def _register(self, pool="demo", host="n1", gpus=4, mem=81920):
        r = self.client.post(
            "/api/nodes/register/",
            {
                "pool": pool,
                "hostname": host,
                "gpu_count": gpus,
                "gpu_memory_mb": mem,
            },
            format="json",
        )
        self.assertIn(r.status_code, (200, 201), r.content)
        return r.json()

    def _heartbeat(self, host="n1"):
        r = self.client.post(
            "/api/nodes/heartbeat/", {"hostname": host}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def _submit(self, pool_id, name, mn, mx, mem, prio):
        r = self.client.post(
            "/api/jobs/",
            {
                "pool": pool_id,
                "name": name,
                "min_gpus": mn,
                "max_gpus": mx,
                "gpu_memory_mb": mem,
                "priority": prio,
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["id"]

    def test_full_api_lifecycle(self):
        pool_id = self._create_pool()
        node = self._register()
        self.assertEqual(node["state"], NodeState.OFFLINE.value)
        hb = self._heartbeat()
        self.assertEqual(hb["state"], NodeState.AVAILABLE.value)

        job_id = self._submit(pool_id, "train-x", 1, 2, 1024, 5)

        r = self.client.post("/api/scheduler/tick/", {}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["placements"], 1)

        r = self.client.get(f"/api/jobs/{job_id}/")
        self.assertEqual(r.json()["state"], JobState.RUNNING)

        r = self.client.get("/api/allocations/")
        self.assertEqual(len(r.json()), 1)

        # Worker completes -> GPUs back.
        r = self.client.post(f"/api/jobs/{job_id}/complete/", {}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], JobState.COMPLETED)
        self.assertEqual(Allocation.objects.count(), 0)

    def test_priority_validation(self):
        pool_id = self._create_pool()
        r = self.client.post(
            "/api/jobs/",
            {
                "pool": pool_id,
                "name": "bad",
                "min_gpus": 1,
                "max_gpus": 1,
                "gpu_memory_mb": 1,
                "priority": 11,
            },
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("priority", str(r.json()))

    def test_min_max_validation(self):
        pool_id = self._create_pool()
        r = self.client.post(
            "/api/jobs/",
            {
                "pool": pool_id,
                "name": "bad",
                "min_gpus": 4,
                "max_gpus": 2,
                "gpu_memory_mb": 1,
                "priority": 5,
            },
            format="json",
        )
        self.assertEqual(r.status_code, 400)

    def test_quota_registration_conflict(self):
        self._create_pool(gpus=2)
        self._register(gpus=2)
        r = self.client.post(
            "/api/nodes/register/",
            {
                "pool": "demo",
                "hostname": "n2",
                "gpu_count": 1,
                "gpu_memory_mb": 81920,
            },
            format="json",
        )
        self.assertEqual(r.status_code, 409)

    def test_cancel_queued_then_complete_conflicts(self):
        pool_id = self._create_pool(gpus=1, jobs=1)
        self._register(gpus=1)
        self._heartbeat()
        job_id = self._submit(pool_id, "j1", 1, 1, 1, 5)
        self.client.post("/api/scheduler/tick/", {}, format="json")
        r = self.client.post(f"/api/jobs/{job_id}/cancel/", {}, format="json")
        self.assertEqual(r.status_code, 200)
        r = self.client.post(f"/api/jobs/{job_id}/complete/", {}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_drain_via_api_blocks_new_placements(self):
        pool_id = self._create_pool()
        node = self._register()
        self._heartbeat()
        node_id = node["id"]
        r = self.client.post(
            f"/api/nodes/{node_id}/state/",
            {"admin_state": "DRAINING"},
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], NodeState.DRAINING.value)
        self._submit(pool_id, "j", 1, 1, 1, 5)
        self.client.post("/api/scheduler/tick/", {}, format="json")
        self.assertEqual(Allocation.objects.count(), 0)

    def test_preemption_roundtrip_via_api(self):
        pool_id = self._create_pool(gpus=2, jobs=8)
        self._register(gpus=2)
        self._heartbeat()
        low_ids = []
        for i in range(2):
            low_ids.append(self._submit(pool_id, f"low{i}", 1, 1, 1, 2))
        self.client.post("/api/scheduler/tick/", {}, format="json")
        urgent_id = self._submit(pool_id, "urgent", 2, 2, 1, 10)
        self.client.post("/api/scheduler/tick/", {}, format="json")

        r = self.client.get("/api/preemptions/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()[0]["status"], "PENDING")
        self.assertEqual(len(r.json()[0]["victims"]), 2)

        # Mock workers acknowledge release.
        for vid in low_ids:
            r = self.client.post(f"/api/jobs/{vid}/release/", {}, format="json")
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(
                r.json()["finish_reason"], FinishReason.PREEMPTED.value
            )
        # One tick confirms release AND places the beneficiary (phase C -> D).
        self.client.post("/api/scheduler/tick/", {}, format="json")

        r = self.client.get(f"/api/jobs/{urgent_id}/")
        self.assertEqual(r.json()["state"], JobState.RUNNING)

        # Audit trail is queryable.
        r = self.client.get("/api/decisions/?kind=PLACED")
        self.assertGreaterEqual(len(r.json()), 3)
        r = self.client.get(f"/api/decisions/?job={urgent_id}")
        kinds = {d["kind"] for d in r.json()}
        self.assertIn("PREEMPT_REQUESTED", kinds)
        self.assertIn("PLACED", kinds)
