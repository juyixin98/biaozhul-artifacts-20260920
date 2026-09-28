"""Coordinator: spawns rank workers, drives steps, publishes one commit.

The coordinator never computes Adam itself; it gathers per-rank flat slices,
assembles the global vectors by declared spans, and writes model + optimizer
state in a single atomic commit.  Restore explicitly refuses a model commit
that differs from the optimizer commit.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import queue as queue_mod
import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import CheckpointError
from .fixtures import AppConfig
from .layout import ParamLayout
from .sharding import ShardPlan
from .training_state import save_commit
from .worker import WorkerJob, worker_main

logger = logging.getLogger("adam_shard.coordinator")


@dataclass(frozen=True)
class StepOutcome:
    step: int
    losses: tuple[float, ...]
    param_flat: np.ndarray
    m_flat: np.ndarray
    v_flat: np.ndarray


@dataclass(frozen=True)
class RunOutcome:
    request_id: str
    world_size: int
    commit_id: str
    step: int
    losses: tuple[float, ...]
    param_flat: np.ndarray
    m_flat: np.ndarray
    v_flat: np.ndarray
    restored_from: str | None


class WorkerFailure(CheckpointError):
    category = "worker_failure"


class ShardProcessPool:
    """Long-lived rank processes communicating over per-rank queues (spawn)."""

    def __init__(self, job_template: WorkerJob) -> None:
        self.job_template = job_template
        self._cmds: list[mp.Queue] = []
        self._results: list[mp.Queue] = []
        self._procs: list[mp.Process] = []

    def __enter__(self) -> "ShardProcessPool":
        ctx = mp.get_context("spawn")
        for rank in range(self.job_template.world_size):
            cmd_q, res_q = ctx.Queue(), ctx.Queue()
            job = WorkerJob(
                root=self.job_template.root,
                rank=rank,
                world_size=self.job_template.world_size,
                dims=self.job_template.dims,
                dtype_name=self.job_template.dtype_name,
                seed=self.job_template.seed,
                n_samples=self.job_template.n_samples,
                adam=self.job_template.adam,
                request_id=self.job_template.request_id,
            )
            proc = ctx.Process(target=worker_main, args=(job, cmd_q, res_q), name=f"adam-rank-{rank}")
            proc.start()
            self._procs.append(proc)
            self._cmds.append(cmd_q)
            self._results.append(res_q)
        return self

    def _collect(self, wait_count: int) -> list[dict[str, Any]]:
        """Round-robin drain every rank's queue until all have replied."""

        replies: list[dict[str, Any] | None] = [None] * wait_count
        pending = set(range(wait_count))
        deadline_seconds = 30.0
        idle_rounds = 0
        max_idle_rounds = int(deadline_seconds / 0.05)
        while pending:
            progressed = False
            for rank in list(pending):
                try:
                    msg = self._results[rank].get(timeout=0.05)
                except queue_mod.Empty:
                    continue
                replies[rank] = msg
                pending.discard(rank)
                progressed = True
            if not progressed:
                idle_rounds += 1
                if idle_rounds > max_idle_rounds:
                    raise WorkerFailure(f"ranks {sorted(pending)} did not reply within {deadline_seconds:.0f}s")
        for msg in replies:
            if not msg.get("ok"):
                raise WorkerFailure(
                    f"[{msg.get('category', 'worker_error')}] {msg.get('message', 'worker failed')}",
                    detail={"category": msg.get("category")},
                )
        return replies  # type: ignore[return-value]

    def initialize(self, mode: str, commit_id: str | None) -> tuple[int, np.ndarray, np.ndarray, np.ndarray]:
        for q in self._cmds:
            q.put({"op": "init", "mode": mode, "commit_id": commit_id})
        replies = self._collect(len(self._procs))
        return self._assemble_replies(replies, want_type="ready")

    def step(self, param_flat: np.ndarray) -> StepOutcome:
        for q in self._cmds:
            q.put({"op": "step", "param_flat": param_flat})
        replies = self._collect(len(self._procs))
        step, p, m, v = self._assemble_replies(replies, want_type="stepped")
        losses = tuple(float(r["loss"]) for r in sorted(replies, key=lambda r: r["rank"]))
        return StepOutcome(step=step, losses=losses, param_flat=p, m_flat=m, v_flat=v)

    @staticmethod
    def _assemble_replies(
        replies: list[dict[str, Any]], *, want_type: str
    ) -> tuple[int, np.ndarray, np.ndarray, np.ndarray]:
        ordered = sorted(replies, key=lambda r: r["rank"])
        ranks_seen = [int(r["rank"]) for r in ordered]
        if ranks_seen != list(range(len(ordered))):
            raise WorkerFailure(f"replies from ranks {ranks_seen}, want 0..{len(ordered) - 1}")
        if any(r.get("type") != want_type for r in ordered):
            raise WorkerFailure(f"expected {want_type!r} replies, got {[r.get('type') for r in ordered]}")
        steps = {int(r["step"]) for r in ordered}
        if len(steps) != 1:
            raise WorkerFailure(f"ranks disagree on step counter: {sorted(steps)}")
        end = max(int(r["end"]) for r in ordered)
        p = np.empty(end, dtype=np.float64)
        m = np.empty(end, dtype=np.float64)
        v = np.empty(end, dtype=np.float64)
        for r in ordered:
            s, e = int(r["start"]), int(r["end"])
            for key, dst in (("param_slice", p), ("m_slice", m), ("v_slice", v)):
                arr = np.asarray(r[key], dtype=np.float64).reshape(-1)
                if arr.shape != (e - s,):
                    raise WorkerFailure(
                        f"rank {r['rank']} {key} has {arr.shape[0]} elems, owns {e - s}"
                    )
                dst[s:e] = arr
        return steps.pop(), p, m, v

    def __exit__(self, *exc) -> None:
        for q in self._cmds:
            q.put({"op": "stop"})
        for proc, q in zip(self._procs, self._results):
            try:
                q.get(timeout=5)
            except queue_mod.Empty:
                pass
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)


def run_sharded_training(
    cfg: AppConfig,
    world_size: int,
    *,
    steps: int | None = None,
    request_id: str | None = None,
    model_commit: str | None = None,
    optim_commit: str | None = None,
) -> RunOutcome:
    """Train ``steps`` updates with ``world_size`` processes; save one commit.

    When restoring, ``model_commit`` and ``optim_commit`` must identify the same
    commit -- the optimizer moments without their matching parameters (or vice
    versa) are refused before any worker state is installed.
    """

    request_id = request_id or f"req-{uuid.uuid4().hex[:12]}"
    steps = cfg.train_steps if steps is None else steps
    spec = cfg.spec
    layout = ParamLayout(tuple(spec.parameter_ids()))
    plan = ShardPlan.create(layout.total_numel, world_size)
    adam_cfg = cfg.adam

    restored_from: str | None = None
    mode = "fresh"
    commit_id: str | None = None
    if model_commit is not None or optim_commit is not None:
        from .training_state import load_commit, require_same_commit

        model_commit = model_commit or optim_commit
        optim_commit = optim_commit or model_commit
        require_same_commit(model_commit, optim_commit)
        # Preflight in the coordinator process: fully verify the commit and the
        # requested re-shard before spawning workers, so typed categories
        # (missing_shard / shape_mismatch / digest_mismatch / ...) surface
        # directly.  Each worker re-verifies its own source shards afterwards.
        load_commit(cfg.storage_root, model_commit, target_world_size=world_size)
        mode, commit_id, restored_from = "restore", model_commit, model_commit

    job = WorkerJob(
        root=cfg.storage_root,
        rank=-1,
        world_size=world_size,
        dims=spec.dims,
        dtype_name=cfg.dtype.name,
        seed=cfg.seed,
        n_samples=cfg.n_samples,
        adam=adam_cfg.to_dict(),
        request_id=request_id,
    )

    logger.info(
        "[req=%s] starting world_size=%s mode=%s commit=%s total_numel=%s sizes=%s",
        request_id, world_size, mode, commit_id, layout.total_numel, plan.sizes(),
    )
    with ShardProcessPool(job) as pool:
        step, param_flat, m_flat, v_flat = pool.initialize(mode, commit_id)
        last_losses: tuple[float, ...] = ()
        while step < steps:
            outcome = pool.step(param_flat)
            step, param_flat, m_flat, v_flat = outcome.step, outcome.param_flat, outcome.m_flat, outcome.v_flat
            last_losses = outcome.losses
            logger.info(
                "[req=%s] coordinator assembled step=%s global_numel=%s rank_losses=%s",
                request_id, step, param_flat.shape[0],
                ", ".join(f"{x:.8f}" for x in last_losses),
            )

    new_commit = f"{request_id}-w{world_size}-s{step}-{uuid.uuid4().hex[:6]}"
    save_commit(
        root=cfg.storage_root,
        commit_id=new_commit,
        layout=layout,
        plan=plan,
        step=step,
        adam_cfg=adam_cfg,
        param_flat=param_flat,
        m_flat=m_flat,
        v_flat=v_flat,
    )
    logger.info("[req=%s] committed model+optim atomically commit=%s", request_id, new_commit)
    return RunOutcome(
        request_id=request_id,
        world_size=world_size,
        commit_id=new_commit,
        step=step,
        losses=last_losses,
        param_flat=param_flat,
        m_flat=m_flat,
        v_flat=v_flat,
        restored_from=restored_from,
    )
