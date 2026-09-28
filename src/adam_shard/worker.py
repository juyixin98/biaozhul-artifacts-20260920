"""Rank worker process: owns one shard of Adam state.

Every worker replicates the full model forward/backward (the standard
data-parallel replica setup on deterministic local data), but only its own
flat slice carries optimizer moments and performs the Adam update.  Slices
are gathered by the coordinator after each step, so distinct processes'
shard states never alias by iteration order.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import numpy as np

from .adam import AdamConfig, AdamShardState, adam_step_slice
from .graph import GraphSpec, MLPModule, mse_loss_and_grad
from .layout import ParamLayout
from .sharding import ShardPlan
from .training_state import CheckpointError, load_rank_view
from .tensor_types import resolve_dtype

logger = logging.getLogger("adam_shard.worker")


@dataclass(frozen=True)
class WorkerJob:
    root: str
    rank: int
    world_size: int
    dims: tuple[int, ...]
    dtype_name: str
    seed: int
    n_samples: int
    adam: dict[str, float]
    request_id: str

    @property
    def spec(self) -> GraphSpec:
        return GraphSpec(dims=self.dims)


def _dataset(job: WorkerJob) -> tuple[np.ndarray, np.ndarray]:
    from .fixtures import make_dataset

    data = make_dataset(job.n_samples, job.dims[0], job.spec, job.seed)
    dtype = resolve_dtype(job.dtype_name)
    return data["x"].astype(dtype), data["y"].astype(dtype)


def _error(exc: BaseException) -> dict[str, Any]:
    category = getattr(exc, "category", "worker_error")
    logger.error("[req=%s][rank=%s] %s: %s", _request_id, _rank, category, exc)
    return {"ok": False, "category": category, "message": str(exc)}


# Process-global context (set inside the child only).
_request_id: str = "-"
_rank: int = -1


def worker_main(job: WorkerJob, cmd_q, result_q) -> None:
    """Entry point running inside a spawned process."""

    global _request_id, _rank
    _request_id, _rank = job.request_id, job.rank
    dtype = resolve_dtype(job.dtype_name)
    cfg = AdamConfig(**job.adam)
    spec = job.spec
    layout = ParamLayout(tuple(spec.parameter_ids()))
    plan = ShardPlan.create(layout.total_numel, job.world_size)
    start, end = plan.span(rank=job.rank)
    model = MLPModule(spec, dtype=dtype, seed=job.seed)
    x, y = _dataset(job)

    param_full = layout.flatten(model.snapshot())
    state = AdamShardState.zeros(end - start, dtype)
    logger.info(
        "[req=%s][rank=%s] pid=%s started world_size=%s owns flat [%s,%s) of %s",
        job.request_id, job.rank, os.getpid(), job.world_size, start, end, layout.total_numel,
    )

    while True:
        cmd = cmd_q.get()
        op = cmd["op"]
        if op == "stop":
            logger.info("[req=%s][rank=%s] stopping", job.request_id, job.rank)
            result_q.put({"ok": True, "type": "stopped"})
            return
        try:
            if op == "init":
                if cmd["mode"] == "restore":
                    view = load_rank_view(job.root, cmd["commit_id"], job.world_size, job.rank)
                    if (view.start, view.end) != (start, end):
                        raise CheckpointError(
                            f"restored slice [{view.start},{view.end}) != assigned [{start},{end})"
                        )
                    state = AdamShardState(m=view.m.astype(dtype), v=view.v.astype(dtype), step=view.step)
                    owned_param = view.param.astype(dtype)
                    logger.info(
                        "[req=%s][rank=%s] restored commit=%s step=%s slice=[%s,%s)",
                        job.request_id, job.rank, cmd["commit_id"], view.step, start, end,
                    )
                    result_q.put({
                        "ok": True, "type": "ready", "rank": job.rank, "step": view.step,
                        "start": start, "end": end,
                        "param_slice": owned_param, "m_slice": view.m, "v_slice": view.v,
                    })
                else:
                    result_q.put({
                        "ok": True, "type": "ready", "rank": job.rank, "step": 0,
                        "start": start, "end": end,
                        "param_slice": param_full[start:end].copy(),
                        "m_slice": state.m.copy(), "v_slice": state.v.copy(),
                    })
            elif op == "step":
                param_full = np.asarray(cmd["param_flat"], dtype=dtype).reshape(-1)
                if param_full.shape[0] != layout.total_numel:
                    raise CheckpointError(
                        f"received {param_full.shape[0]} params, layout expects {layout.total_numel}"
                    )
                # Install by stable identity; deliberately tolerate reordered dicts.
                model.install(layout.unflatten(param_full))
                pred = model.forward(x)
                loss, grad_out = mse_loss_and_grad(pred, y)
                grads = model.backward(x, grad_out)
                grad_flat = layout.flatten(grads)
                p_new, m_new, v_new, step_new = adam_step_slice(
                    param_full[start:end], grad_flat[start:end], state, cfg
                )
                state = AdamShardState(m=m_new, v=v_new, step=step_new)
                logger.info(
                    "[req=%s][rank=%s] step=%s loss=%.10f updated [%s,%s)",
                    job.request_id, job.rank, step_new, loss, start, end,
                )
                result_q.put({
                    "ok": True, "type": "stepped", "rank": job.rank, "step": step_new,
                    "loss": loss, "start": start, "end": end,
                    "param_slice": np.ascontiguousarray(p_new),
                    "m_slice": np.ascontiguousarray(m_new),
                    "v_slice": np.ascontiguousarray(v_new),
                })
            else:
                result_q.put(_error(ValueError(f"unknown op {op!r}")))
        except Exception as exc:  # surfaced categorically to the coordinator
            result_q.put(_error(exc))
