"""真实多进程集成测试：uvicorn 子进程 + 独立工作者子进程 + HTTP。

这些测试真正派生进程（不是 ASGI 模拟），验证跨进程的布局同步、JSON 编解码、
心跳与中断判定。标记为 integration，可用 ``pytest -m integration`` 选择。
"""

from __future__ import annotations

import pytest

from gradbucket.cluster import WorkerSpec, run_cluster

pytestmark = pytest.mark.integration


def test_real_cluster_happy_path_unequal_shards():
    result = run_cluster(
        n_workers=3,
        n_samples=12,
        shard_sizes=[2, 3, 7],
        bucket_capacity=2,
        liveness_timeout=3.0,
        port=8791,
        log_path="logs/test_cluster_happy.log",
    )
    seal = result["seal"]
    assert seal["code"] == "ACCEPTED"
    assert seal["commit"]["generation_after"] == 1
    assert result["worker_return_codes"] == {"w0": 0, "w1": 0, "w2": 0}
    slot_w = seal["commit"]["bucket_bases"][0]["slots"][0]
    assert slot_w["sample_counts"] == [2, 3, 7]
    assert slot_w["weight_total"] == pytest.approx(12.0)
    verdicts = result["diagnostics"]["verdicts"]
    assert "REJECTED_WORKER_LOST" not in verdicts


def test_real_cluster_worker_crash_after_first_bucket_rejects_round():
    specs = [
        WorkerSpec("w0", 0),
        WorkerSpec("w1", 1),
        WorkerSpec("w2", 2, fail_after_bucket=0),
    ]
    result = run_cluster(
        n_workers=3,
        n_samples=12,
        shard_sizes=[2, 3, 7],
        workers=specs,
        bucket_capacity=2,
        liveness_timeout=1.5,
        port=8792,
        log_path="logs/test_cluster_lost.log",
    )
    seal = result["seal"]
    # 崩溃工作者非零退出码被如实上报。
    assert result["worker_return_codes"]["w2"] == 3
    assert seal["code"] == "REJECTED_WORKER_LOST"
    assert "commit" not in seal
    assert seal["round_view"]["status"] == "REJECTED"
    verdicts = result["diagnostics"]["verdicts"]
    assert verdicts.get("REJECTED_WORKER_LOST", 0) >= 1


def test_real_cluster_withheld_bucket_is_indeterminate_not_rejected():
    specs = [
        WorkerSpec("w0", 0),
        WorkerSpec("w1", 1),
        WorkerSpec("w2", 2, withhold_bucket=1, linger_seconds=5.0),
    ]
    result = run_cluster(
        n_workers=3,
        n_samples=12,
        shard_sizes=[2, 3, 7],
        workers=specs,
        bucket_capacity=2,
        liveness_timeout=1.5,
        port=8793,
        log_path="logs/test_cluster_pending.log",
    )
    seal = result["seal"]
    # 工作者仍在心跳，只是压下了桶 1。
    assert seal["code"] == "INDETERMINATE_PENDING"
    assert seal["round_view"]["status"] == "OPEN"
    assert seal["round_view"]["missing_buckets"] == [1]
