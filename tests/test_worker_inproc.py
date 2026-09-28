"""In-process worker tests: drive ``worker_main`` from a thread via real queues.

This covers the worker logic directly (including restore-time ``load_rank_view``
re-sharding) without depending on spawned subprocess coverage collection.
"""

from __future__ import annotations

import queue as thread_queue
import threading

import numpy as np

from adam_shard.adam import AdamShardState, adam_step_slice
from adam_shard.fixtures import make_dataset
from adam_shard.graph import MLPModule, mse_loss_and_grad
from adam_shard.layout import ParamLayout
from adam_shard.sharding import ShardPlan
from adam_shard.training_state import load_commit, save_commit
from adam_shard.worker import WorkerJob, worker_main


def _start_worker(job: WorkerJob):
    cmd_q: thread_queue.Queue = thread_queue.Queue()
    res_q: thread_queue.Queue = thread_queue.Queue()
    thread = threading.Thread(target=worker_main, args=(job, cmd_q, res_q), daemon=True)
    thread.start()
    return cmd_q, res_q, thread


def _job(cfg, world_size: int, rank: int, request_id: str = "req-worker") -> WorkerJob:
    return WorkerJob(
        root=cfg.storage_root,
        rank=rank,
        world_size=world_size,
        dims=cfg.spec.dims,
        dtype_name=np.dtype(cfg.dtype).name,
        seed=cfg.seed,
        n_samples=cfg.n_samples,
        adam=cfg.adam.to_dict(),
        request_id=request_id,
    )


def _seed_commit(cfg, commit_id: str, world_size: int, step: int, seed: int = 5):
    layout = ParamLayout(tuple(cfg.spec.parameter_ids()))
    plan = ShardPlan.create(layout.total_numel, world_size)
    rng = np.random.default_rng(seed)
    n = layout.total_numel
    save_commit(
        root=cfg.storage_root,
        commit_id=commit_id,
        layout=layout,
        plan=plan,
        step=step,
        adam_cfg=cfg.adam,
        param_flat=rng.standard_normal(n),
        m_flat=rng.standard_normal(n) * 0.1,
        v_flat=np.abs(rng.standard_normal(n)) * 0.01 + 1e-6,
    )
    return layout


def test_worker_fresh_init_step_stop(app_config):
    cfg = app_config
    job = _job(cfg, world_size=2, rank=1)  # owns [29,59)
    cmd_q, res_q, thread = _start_worker(job)

    cmd_q.put({"op": "init", "mode": "fresh", "commit_id": None})
    ready = res_q.get(timeout=10)
    assert ready["ok"] is True and ready["type"] == "ready"
    assert (ready["start"], ready["end"]) == (29, 59)
    assert ready["step"] == 0

    # First step from zero state: compare against a direct slice update.
    layout = ParamLayout(tuple(cfg.spec.parameter_ids()))
    model = MLPModule(cfg.spec, dtype=cfg.dtype, seed=cfg.seed)
    data = make_dataset(cfg.n_samples, cfg.spec.dims[0], cfg.spec, cfg.seed)
    param_full = layout.flatten(model.snapshot())
    cmd_q.put({"op": "step", "param_flat": param_full})
    stepped = res_q.get(timeout=10)
    assert stepped["ok"] is True and stepped["step"] == 1

    pred = model.forward(data["x"])
    _, grad_out = mse_loss_and_grad(pred, data["y"])
    grad_flat = layout.flatten(model.backward(data["x"], grad_out))
    state0 = AdamShardState.zeros(30, cfg.dtype)
    exp_p, exp_m, exp_v, exp_step = adam_step_slice(
        param_full[29:59], grad_flat[29:59], state0, cfg.adam
    )
    np.testing.assert_allclose(stepped["param_slice"], exp_p, rtol=1e-12)
    np.testing.assert_allclose(stepped["m_slice"], exp_m, rtol=1e-12)
    np.testing.assert_allclose(stepped["v_slice"], exp_v, rtol=1e-12)
    assert exp_step == 1

    cmd_q.put({"op": "stop"})
    assert res_q.get(timeout=10)["type"] == "stopped"
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_worker_restore_reshard_2_to_3_middle_rank_then_step(app_config):
    cfg = app_config
    layout = _seed_commit(cfg, "seed-w2", world_size=2, step=2)
    # Rank 1 of a 3-way split owns [19,38): it crosses the 2-way boundary 29,
    # so restore gathers from BOTH source shards (two copy segments).
    job = _job(cfg, world_size=3, rank=1, request_id="req-worker-restore")
    cmd_q, res_q, thread = _start_worker(job)

    cmd_q.put({"op": "init", "mode": "restore", "commit_id": "seed-w2"})
    ready = res_q.get(timeout=10)
    assert ready["ok"] is True
    assert ready["step"] == 2
    assert (ready["start"], ready["end"]) == (19, 38)

    globals_ = load_commit(cfg.storage_root, "seed-w2")
    np.testing.assert_allclose(ready["param_slice"], globals_.param_flat[19:38], rtol=1e-12)
    np.testing.assert_allclose(ready["m_slice"], globals_.m_flat[19:38], rtol=1e-12)
    np.testing.assert_allclose(ready["v_slice"], globals_.v_flat[19:38], rtol=1e-12)

    # One step continues from step 2 -> 3 using the restored moments.
    cmd_q.put({"op": "step", "param_flat": globals_.param_flat})
    stepped = res_q.get(timeout=10)
    assert stepped["ok"] is True and stepped["step"] == 3

    cmd_q.put({"op": "stop"})
    res_q.get(timeout=10)
    thread.join(timeout=5)


def test_worker_unknown_op_returns_typed_error(app_config):
    cfg = app_config
    cmd_q, res_q, thread = _start_worker(_job(cfg, world_size=1, rank=0))
    cmd_q.put({"op": "init", "mode": "fresh", "commit_id": None})
    res_q.get(timeout=10)
    cmd_q.put({"op": "frobnicate"})
    reply = res_q.get(timeout=10)
    assert reply["ok"] is False
    assert reply["category"] == "worker_error"
    cmd_q.put({"op": "stop"})
    res_q.get(timeout=10)
    thread.join(timeout=5)


def test_worker_step_with_wrong_param_count_rejected(app_config):
    cfg = app_config
    cmd_q, res_q, thread = _start_worker(_job(cfg, world_size=2, rank=0))
    cmd_q.put({"op": "init", "mode": "fresh", "commit_id": None})
    res_q.get(timeout=10)
    cmd_q.put({"op": "step", "param_flat": np.zeros(5)})
    reply = res_q.get(timeout=10)
    assert reply["ok"] is False
    assert reply["category"] == "checkpoint_error"
    cmd_q.put({"op": "stop"})
    res_q.get(timeout=10)
    thread.join(timeout=5)
