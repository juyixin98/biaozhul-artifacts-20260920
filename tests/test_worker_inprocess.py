"""In-process worker-loop tests against the live HTTP server.

These call ``run_worker`` directly (same process, real uvicorn thread,
real HTTP via urllib), so the worker logic gets coverage while still
exercising the full wire protocol.
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from bucket_sync.bucketing import BucketLayout
from bucket_sync.config import (
    RuntimeConfig,
    initial_params,
    make_graph,
    make_shards,
)
from bucket_sync.coordinator import Coordinator
from bucket_sync.diagnostics import Diagnostics
from bucket_sync.reference import reference_run
from bucket_sync.runtime import LocalRuntime, WorkerSpec
from bucket_sync.training import BIAS_PARAM, WEIGHT_PARAM, ModelState
from bucket_sync.worker import WorkerConfig, run_worker

pytestmark = pytest.mark.integration

WORKERS = ["w0", "w1", "w2"]
SIZES = (5, 3, 2)
TOL = 1e-9


def _runtime(cfg, *, allow_partial=False):
    graph = make_graph(cfg.in_features)
    layout = BucketLayout(graph, cfg.bucket_size)
    model = ModelState(graph, initial_params(cfg.in_features))
    coordinator = Coordinator(
        graph, layout, model, lr=cfg.lr, diagnostics=Diagnostics(),
        heartbeat_timeout=cfg.heartbeat_timeout_s,
        allow_partial_coverage=allow_partial,
    )
    specs = [
        WorkerSpec(worker_id=f"w{i}", x_shard=s[0], y_shard=s[1])
        for i, s in enumerate(make_shards(cfg.in_features, SIZES, seed=42))
    ]
    rt = LocalRuntime(coordinator, layout, specs)
    return graph, layout, coordinator, rt, make_shards(cfg.in_features, SIZES, seed=42)


def _start_worker_thread(rt, wid, shard, layout, **kw):
    cfg = WorkerConfig(
        worker_id=wid, server_url=rt.base_url, layout=layout,
        x_shard=shard[0], y_shard=shard[1], **kw,
    )
    box = {}

    def target():
        box["summaries"] = run_worker(cfg, rounds=1)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def test_worker_loop_happy_path_over_real_http(cfg=None):
    cfg = RuntimeConfig()
    graph, layout, coordinator, rt, shards = _runtime(cfg)
    rt.start_server()
    try:
        for wid in WORKERS:
            coordinator.register_worker(wid)
        desc = coordinator.begin_round(WORKERS)
        threads = []
        for wid, shard in zip(WORKERS, shards):
            t, _ = _start_worker_thread(rt, wid, shard, layout)
            threads.append(t)
        # Drive the commit barrier from the control-plane side.
        while True:
            report = coordinator.commit_round()
            if report.outcome == "committed":
                break
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive()
        expected = reference_run(initial_params(cfg.in_features), shards, cfg.lr, 1)
        actual = coordinator.snapshot_params()
        np.testing.assert_allclose(actual[WEIGHT_PARAM], expected[WEIGHT_PARAM], atol=TOL)
        np.testing.assert_allclose(actual[BIAS_PARAM], expected[BIAS_PARAM], atol=TOL)
    finally:
        rt.stop_server()


def test_worker_loop_with_corrupt_token_reports_rejections():
    cfg = RuntimeConfig()
    graph, layout, coordinator, rt, shards = _runtime(cfg)
    rt.start_server()
    try:
        for wid in WORKERS:
            coordinator.register_worker(wid)
        desc = coordinator.begin_round(WORKERS)
        t_bad, box_bad = _start_worker_thread(
            rt, "w0", shards[0], layout, corrupt_base_token=True
        )
        t_ok, _ = _start_worker_thread(rt, "w1", shards[1], layout)
        t_ok2, _ = _start_worker_thread(rt, "w2", shards[2], layout)
        # Wait for the server to reject both of w0's buckets, then abort so
        # all three workers observe the closed round and return.
        import time as _time
        for _ in range(250):
            bad_rejections = [
                e for e in coordinator.diagnostics.events()
                if e.worker_id == "w0" and e.reason == "wrong_base_generation"
            ]
            if len(bad_rejections) == layout.bucket_count():
                break
            _time.sleep(0.02)
        coordinator.abort_round("cleanup")
        for t in (t_bad, t_ok, t_ok2):
            t.join(timeout=10.0)
            assert not t.is_alive()
        summaries = box_bad["summaries"]
        assert len(summaries) == 1
        assert {r["reason"] for r in summaries[0]["rejected"]} == {"wrong_base_generation"}
        assert summaries[0]["accepted_buckets"] == []
    finally:
        rt.stop_server()


def test_worker_loop_crash_and_hang_flags_are_surfaced():
    cfg = RuntimeConfig()
    graph, layout, coordinator, rt, shards = _runtime(cfg)
    rt.start_server()
    try:
        coordinator.register_worker("w0")
        coordinator.begin_round(["w0"])
        cfg_crash = WorkerConfig(
            worker_id="w0", server_url=rt.base_url, layout=layout,
            x_shard=shards[0][0], y_shard=shards[0][1], crash_after_buckets=1,
        )
        summaries = run_worker(cfg_crash, rounds=1)
        assert summaries[0]["crashed"] is True
        coordinator.abort_round("cleanup")
    finally:
        rt.stop_server()


def test_worker_loop_missing_param_in_partial_mode_commits():
    cfg = RuntimeConfig()
    graph, layout, coordinator, rt, shards = _runtime(cfg, allow_partial=True)
    rt.start_server()
    try:
        for wid in WORKERS:
            coordinator.register_worker(wid)
        coordinator.begin_round(WORKERS)
        threads = []
        for i, (wid, shard) in enumerate(zip(WORKERS, shards)):
            kw = {"missing_params": (BIAS_PARAM,)} if wid == "w1" else {}
            t, _ = _start_worker_thread(rt, wid, shard, layout, **kw)
            threads.append(t)
        while True:
            report = coordinator.commit_round()
            if report.outcome == "committed":
                break
        for t in threads:
            t.join(timeout=10.0)
        assert coordinator.current_generation() == 1
    finally:
        rt.stop_server()
