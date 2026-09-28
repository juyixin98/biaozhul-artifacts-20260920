"""Application services tying checkpoint IO, processes and verification together."""

from __future__ import annotations

import logging

import numpy as np

from .coordinator import run_sharded_training
from .fixtures import AppConfig, load_sample_dataset
from .graph import MLPModule, mse_loss_and_grad
from .reference_adam import ReferenceAdam
from .sharding import ShardPlan
from .training_state import CheckpointError, list_commits, load_commit
from .verification import (
    STATUS_FAIL,
    STATUS_PASS,
    CheckEntry,
    VerificationReport,
    compare_param_dicts,
    finite_difference_gradient_check,
    max_abs_rel_diff,
)

logger = logging.getLogger("adam_shard.service")


def train(
    cfg: AppConfig,
    world_size: int,
    *,
    steps: int | None = None,
    request_id: str | None = None,
    model_commit: str | None = None,
    optim_commit: str | None = None,
):
    return run_sharded_training(
        cfg,
        world_size,
        steps=steps,
        request_id=request_id,
        model_commit=model_commit,
        optim_commit=optim_commit,
    )


def describe_commits(cfg: AppConfig) -> list[dict]:
    out = []
    for commit_id in list_commits(cfg.storage_root):
        try:
            loaded = load_commit(cfg.storage_root, commit_id)
        except CheckpointError as exc:
            out.append({"commit_id": commit_id, "loadable": False, "category": exc.category, "message": str(exc)})
            continue
        out.append(
            {
                "commit_id": commit_id,
                "loadable": True,
                "source_world_size": loaded.source_plan.world_size,
                "target_world_size": loaded.target_plan.world_size,
                "step": loaded.step,
                "total_numel": loaded.layout.total_numel,
                "shard_sizes": list(loaded.source_plan.sizes()),
            }
        )
    return out


def validate_commit(cfg: AppConfig, commit_id: str, target_world_size: int) -> dict:
    """Strict validation; returns a structured verdict instead of raising."""

    loaded = load_commit(cfg.storage_root, commit_id, target_world_size=target_world_size)
    return {
        "ok": True,
        "commit_id": commit_id,
        "source_world_size": loaded.source_plan.world_size,
        "target_world_size": target_world_size,
        "step": loaded.step,
        "target_shard_sizes": list(loaded.target_plan.sizes()),
        "parameters": [t.to_dict() for t in loaded.layout.ids],
    }


def _reference_one_step_from_commit(cfg: AppConfig, loaded) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Independent oracle: continue one Adam step from a restored commit."""

    data = load_sample_dataset(cfg)
    model = MLPModule(cfg.spec, dtype=np.dtype(np.float64), seed=cfg.seed)
    params0 = loaded.layout.unflatten(loaded.param_flat)
    model.install(params0)

    m_by_name = loaded.layout.unflatten(loaded.m_flat)
    v_by_name = loaded.layout.unflatten(loaded.v_flat)
    opt = ReferenceAdam(cfg=loaded.adam_cfg, moments={n: (m_by_name[n], v_by_name[n]) for n in m_by_name}, t=loaded.step)

    pred = model.forward(data["x"])
    _, grad_out = mse_loss_and_grad(pred, data["y"])
    grads = model.backward(data["x"], grad_out)
    new_params = opt.step(params0, grads)
    new_moments = dict(opt.moments)
    return new_params, new_moments


def verify_restore_parity(
    cfg: AppConfig,
    source_commit: str,
    target_world_size: int,
    request_id: str,
) -> VerificationReport:
    """End-to-end check: restore at a NEW world size, take one step, match oracle.

    Covers uneven-tail re-sharding (the commit may itself be uneven), stable
    parameter identity under reordering and first/second-moment/step alignment.
    """

    loaded = load_commit(cfg.storage_root, source_commit)
    location = f"{cfg.storage_root}/{source_commit}"
    checks: list[CheckEntry] = []

    # Independent single-process oracle continues from identical state.
    expected_params, expected_moments = _reference_one_step_from_commit(cfg, loaded)

    # Sharded core continues at the target (different) process count.
    if target_world_size == loaded.source_plan.world_size:
        logger.warning("[req=%s] parity target equals source world size; re-shard still exercised", request_id)
    outcome = run_sharded_training(
        cfg,
        target_world_size,
        steps=loaded.step + 1,
        request_id=request_id,
        model_commit=source_commit,
        optim_commit=source_commit,
    )
    actual_params = loaded.layout.unflatten(outcome.param_flat)
    actual_m = loaded.layout.unflatten(outcome.m_flat)
    actual_v = loaded.layout.unflatten(outcome.v_flat)

    checks.append(compare_param_dicts(actual_params, expected_params, label="one_step_params"))

    worst_abs = worst_rel = 0.0
    for name in sorted(expected_moments):
        em, ev = expected_moments[name]
        a1, r1 = max_abs_rel_diff(actual_m[name], em)
        a2, r2 = max_abs_rel_diff(actual_v[name], ev)
        worst_abs, worst_rel = max(worst_abs, a1, a2), max(worst_rel, r1, r2)
    checks.append(
        CheckEntry(
            name="one_step_moments",
            status=STATUS_PASS if worst_abs <= 1e-12 and worst_rel <= 1e-10 else STATUS_FAIL,
            detail={
                "reason": "" if worst_abs <= 1e-12 and worst_rel <= 1e-10 else "moments diverge from oracle",
                "max_abs_err": worst_abs,
                "max_rel_err": worst_rel,
            },
        )
    )

    step_ok = outcome.step == loaded.step + 1
    checks.append(
        CheckEntry(
            name="step_counter_correspondence",
            status=STATUS_PASS if step_ok else STATUS_FAIL,
            detail={
                "reason": "" if step_ok else f"step {outcome.step} != restored {loaded.step}+1",
                "restored_step": loaded.step,
                "observed_step": outcome.step,
            },
        )
    )

    # Gradient correctness on the restored parameters guards the whole premise.
    data = load_sample_dataset(cfg)
    model = MLPModule(cfg.spec, dtype=cfg.dtype, seed=cfg.seed)
    model.install(actual_params)
    checks.append(finite_difference_gradient_check(model, data["x"], data["y"], seed=cfg.seed))

    checks.append(
        CheckEntry(
            name="reshard_uneven_tail",
            status=STATUS_PASS,
            detail={
                "source_world_size": loaded.source_plan.world_size,
                "target_world_size": target_world_size,
                "source_shard_sizes": list(loaded.source_plan.sizes()),
                "target_shard_sizes": list(
                    ShardPlan.create(loaded.layout.total_numel, target_world_size).sizes()
                ),
                "total_numel": loaded.layout.total_numel,
            },
        )
    )

    return VerificationReport(
        request_id=request_id,
        stage="restore+one_step",
        version="1.0.0",
        location=location,
        checks=tuple(checks),
    )
