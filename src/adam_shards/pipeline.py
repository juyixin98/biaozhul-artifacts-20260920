"""End-to-end orchestration: train, shard, restore, reshard, verify."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .adam import Adam, AdamConfig, OptimState
from .graph import GraphSpec, MLPModel, synthetic_dataset
from .sharding import restore_arrays, save_checkpoint
from .verification import (
    VerificationReport,
    compare_states,
    finite_difference_check,
    reference_adam_run,
)


@dataclass(frozen=True)
class PipelineResult:
    request_id: str
    commit_id: str
    save_world_size: int
    restore_world_size: int
    step_after_restore: int
    fd_max_rel_error: float
    param_report: VerificationReport
    m1_report: VerificationReport
    m2_report: VerificationReport

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "commit_id": self.commit_id,
            "save_world_size": self.save_world_size,
            "restore_world_size": self.restore_world_size,
            "step_after_restore": self.step_after_restore,
            "finite_difference_max_rel_error": self.fd_max_rel_error,
            "parameter_comparison": self.param_report.to_dict(),
            "moment1_comparison": self.m1_report.to_dict(),
            "moment2_comparison": self.m2_report.to_dict(),
            "passed": all((
                self.param_report.passed,
                self.m1_report.passed,
                self.m2_report.passed,
            )),
        }


def make_batches(x: np.ndarray, y: np.ndarray, batch_size: int,
                 n_steps: int) -> list[tuple[np.ndarray, np.ndarray]]:
    batches = []
    for step in range(n_steps):
        lo = (step * batch_size) % (len(x) - batch_size + 1)
        batches.append((x[lo:lo + batch_size], y[lo:lo + batch_size]))
    return batches


def moments_from_state(state: OptimState) -> dict[str, tuple[np.ndarray, np.ndarray, int]]:
    return {n: (state.get(n).m, state.get(n).v, state.get(n).step) for n in state.names}


def train(model: MLPModel, optimizer: Adam, state: OptimState,
          batches: list[tuple[np.ndarray, np.ndarray]]) -> None:
    for x, y in batches:
        _, cache = model.forward(x)
        grads = model.backward(x, y, cache)
        optimizer.step(model.params, state, grads)


def _reordered_shapes(spec: GraphSpec, permutation_seed: int) -> dict[str, tuple[int, ...]]:
    """Target graph identity presented in a deliberately scrambled order."""
    ids = spec.param_ids()
    rng = np.random.default_rng(permutation_seed)
    order = rng.permutation(len(ids)).tolist()
    return {ids[i].name: ids[i].shape for i in order}


def run_pipeline(
    *,
    dims: tuple[int, ...],
    seed: int,
    n_samples: int,
    batch_size: int,
    train_steps: int,
    adam_cfg: AdamConfig,
    save_world_size: int,
    restore_world_size: int,
    ckpt_dir: str,
    request_id: str,
    tol: float,
    logger,
) -> PipelineResult:
    """Train ``train_steps`` updates, shard on 2 procs, restore on 3 procs with
    a reordered parameter identity, take one more update, and compare every
    state tensor against the independent unsharded reference.
    """
    spec = GraphSpec(dims)
    x, y = synthetic_dataset(n_samples, dims[0], dims[-1], seed=seed)
    # Deterministically distinct batches: train_steps + one post-restore step.
    batches = make_batches(x, y, batch_size, train_steps + 1)

    model = MLPModel(spec, seed=seed)
    init = {k: v.copy() for k, v in model.named_params().items()}
    state = OptimState({n: spec.shape_of(n) for n in spec.param_names})
    optimizer = Adam(adam_cfg)

    # Independent oracle for the core gradients at the benign init point
    # (central finite differences). Done before training so no ReLU unit sits
    # exactly on a kink, which would make the numeric oracle itself ill-defined.
    x0, y0 = batches[0]
    _, cache0 = model.forward(x0)
    grads0 = model.backward(x0, y0, cache0)
    fd_err, fd_notes = finite_difference_check(
        lambda p: _standalone_loss(p, x0, y0, len(spec.dims) - 1),
        model.named_params(), grads0,
    )
    logger.info("finite-difference gradient check max_rel_err=%.3e", fd_err)

    train(model, optimizer, state, batches[:train_steps])

    commit_id = save_checkpoint(
        ckpt_dir, model.named_params(), moments_from_state(state),
        save_world_size, request_id=request_id, logger=logger,
    )

    # Restore under a different process count AND scrambled parameter order.
    target_shapes = _reordered_shapes(spec, permutation_seed=seed + 999)
    loaded = restore_arrays(
        ckpt_dir, restore_world_size,
        target_shapes=target_shapes, request_id=request_id, logger=logger,
    )

    restored_model = MLPModel(spec, seed=seed)
    restored_model.set_params(loaded["params"])  # name-keyed, order-independent
    restored_state = OptimState({n: spec.shape_of(n) for n in spec.param_names})
    for name, (mm, vv, step) in loaded["moments"].items():
        restored_state.set(name, mm, vv, step)
    train(restored_model, optimizer, restored_state, [batches[train_steps]])
    final_step = restored_state.global_step()

    # Independent reference answer for all train_steps+1 updates (unsharded).
    ref = reference_adam_run(
        init, batches, n_layers=len(spec.dims) - 1,
        lr=adam_cfg.lr, beta1=adam_cfg.beta1, beta2=adam_cfg.beta2, eps=adam_cfg.eps,
    )
    uncertainties = list(fd_notes)
    if loaded["source_world_size"] == restore_world_size:
        uncertainties.append("restore used same process count as save; "
                             "resharding path not exercised")
    param_report = compare_states(
        restored_model.named_params(), ref["params"],
        request_id=request_id, stage="post_restore_step_params", tol=tol,
        uncertainties=uncertainties,
    )
    m1 = {n: restored_state.get(n).m for n in restored_state.names}
    m2 = {n: restored_state.get(n).v for n in restored_state.names}
    ref_m1 = {n: ref["moments"][n][0] for n in ref["moments"]}
    ref_m2 = {n: ref["moments"][n][1] for n in ref["moments"]}
    m1_report = compare_states(
        m1, ref_m1, request_id=request_id,
        stage="post_restore_step_moment1", tol=tol,
    )
    m2_report = compare_states(
        m2, ref_m2, request_id=request_id,
        stage="post_restore_step_moment2", tol=tol,
    )
    return PipelineResult(
        request_id=request_id, commit_id=commit_id,
        save_world_size=save_world_size,
        restore_world_size=restore_world_size,
        step_after_restore=final_step,
        fd_max_rel_error=fd_err,
        param_report=param_report, m1_report=m1_report, m2_report=m2_report,
    )


def _standalone_loss(params: dict[str, np.ndarray], x: np.ndarray,
                     y: np.ndarray, n_layers: int) -> float:
    """Loss callable used by finite differences (independent of model class)."""
    from .verification import _ref_loss  # local import keeps oracle self-contained
    return _ref_loss(params, x, y, n_layers)
