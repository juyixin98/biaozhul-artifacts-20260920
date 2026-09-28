"""Plan execution: forward, checkpointed reverse with replay, gradients.

The executor is a *second, independent* implementation of the memory event
sequence modelled by :mod:`app.core.memory`: it consumes the same
:class:`~app.core.memory.BackwardSchedule` while running real numpy
evaluation and maintaining its own live-buffer tracker, then asserts the
measured peak and recompute cost equal the simulation.

Replay guarantees
-----------------
* stochastic ops regenerate the exact forward masks:

  - ``counter``  : per-node deterministic generator; replay recreates it so
    the first draw is the forward draw;
  - ``snapshot`` : the pre-forward MT19937 state is restored per replay wave
    and advanced (dummy draws) to the required dropout ordinal.
* ``external`` nodes emit their non-idempotent side effect exactly once
  (forward); replay re-evaluates numerically with ``emit=False``.
* shared subgraphs are memoised: each activation has one production event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np

from . import ops as ops_mod
from .errors import ComputationError, InvalidInputError
from .graph import ValidatedGraph
from .memory import RNG_STATE_ELEMENTS, build_backward_schedule
from .planner import CheckpointPlan
from .rng import RandomStream
from .state import RunState, RunStatus, TrainingState
from .tensor import SUPPORTED_DTYPE


@dataclass
class _LiveTracker:
    """Independent live-buffer accounting during real execution."""

    sizes: Mapping[str, int]
    scratch_sizes: Mapping[str, int]
    live: Dict[str, int] = field(default_factory=dict)
    peak: int = 0
    workspace_peak: int = 0

    def add(self, key: str, size: int) -> None:
        self.live[key] = size
        self._tick(0)

    def drop(self, key: str) -> None:
        self.live.pop(key, None)

    def tick_scratch(self, size: int) -> None:
        self._tick(size)
        self.workspace_peak = max(self.workspace_peak, size)

    def _tick(self, scratch: int) -> None:
        total = sum(self.live.values()) + scratch
        self.peak = max(self.peak, total)


def execute(
    g: ValidatedGraph,
    plan: CheckpointPlan,
    state: TrainingState,
    inputs: Mapping[str, np.ndarray],
    *,
    run_id: str,
    master_seed: int = 1234,
    rng_strategy: str = "counter",
) -> Tuple[RunState, Dict[str, Any]]:
    """Execute one training step. Returns (run state, replay log)."""

    roots = _prepare_roots(g, state, inputs)
    sizes, scratch_sizes, costs, vjp_costs = _cost_tables(g)
    tracker = _LiveTracker(sizes, scratch_sizes)
    stream = RandomStream(master_seed, strategy=rng_strategy)
    rng_snapshot = stream.snapshot()

    retained = frozenset(plan.retained)
    schedule = build_backward_schedule(g, plan.retained)
    dropout_order = [nid for nid in g.order
                     if g.nodes[nid].op == ops_mod.DROPOUT]
    dropout_ordinal = {nid: i for i, nid in enumerate(dropout_order)}
    has_stochastic = bool(dropout_order)
    if has_stochastic and rng_strategy == "snapshot":
        tracker.add("rng:snapshot", RNG_STATE_ELEMENTS)

    replay_log: Dict[str, Any] = {
        "run_id": run_id,
        "phases": [],
        "external_emits": [],
        "replay_waves": [],
        "rng_strategy": rng_strategy,
    }

    # ---------------------------------------------------------------- forward
    values: Dict[str, np.ndarray] = dict(roots)
    fwd_refs = dict(g.user_count)
    for nid in g.order:
        node = g.nodes[nid]
        if node.op in ops_mod.ROOT_OPS:
            tracker.add(f"root:{nid}", sizes[nid])
            continue
        tracker.tick_scratch(scratch_sizes[nid])
        out, _ = _eval_forward(g, nid, values, stream, emit=True,
                               replay_log=replay_log)
        _require_finite(out, nid, "forward")
        values[nid] = out
        tracker.add(f"act:{nid}", sizes[nid])
        for u in node.inputs:
            if g.nodes[u].op in ops_mod.ROOT_OPS:
                continue
            fwd_refs[u] -= 1
            if fwd_refs[u] == 0 and u not in retained:
                values.pop(u, None)
                tracker.drop(f"act:{u}")
    replay_log["phases"].append("forward_complete")

    target = g.target
    if sizes[target] != 1:
        raise InvalidInputError(
            "training requires a scalar (numel==1) target, use reduce_sum",
            code="E_LOSS_NOT_SCALAR",
            context={"target": target, "numel": sizes[target]},
        )
    loss = float(values[target].reshape(()).item())

    # ---------------------------------------------------------------- backward
    grads: Dict[str, np.ndarray] = {
        target: np.ones((1,), dtype=SUPPORTED_DTYPE)
    }
    tracker.add(f"grad:{target}", sizes[target])
    # The target activation has no backward readers.
    if schedule.reads.get(target, 0) == 0:
        values.pop(target, None)
        tracker.drop(f"act:{target}")
    param_grads: Dict[str, np.ndarray] = {}
    input_grads: Dict[str, np.ndarray] = {}
    materialised: Dict[str, np.ndarray] = {}
    held_masks: Dict[str, np.ndarray] = {}
    remaining_reads = dict(schedule.reads)
    recomputed: List[str] = []
    recompute_flops = 0
    bwd_flops = 0

    def act_value(nid: str) -> np.ndarray:
        if nid in values:
            return values[nid]
        return materialised[nid]

    def release(x: str) -> None:
        tracker.drop(f"act:{x}")
        values.pop(x, None)
        materialised.pop(x, None)

    cursor = 0

    def restore_and_catch_up(ordinal: int) -> None:
        """Snapshot strategy: position the stream just before dropout k."""

        nonlocal cursor
        if rng_strategy != "snapshot":
            return
        while cursor < ordinal:
            skip = dropout_order[cursor]
            stream.draw_dummy(g.shapes[skip], g.nodes[skip].params["p"])
            cursor += 1

    for step in schedule.steps:
        w = step.node
        draws_in_wave = bool(step.recompute) and any(
            g.nodes[v].op == ops_mod.DROPOUT for v in step.recompute
        )
        if (draws_in_wave or step.mask_replays) and rng_strategy == "snapshot":
            stream.restore(rng_snapshot)
            cursor = 0
        wave_record: Optional[Dict[str, Any]] = None
        if step.recompute or step.mask_replays:
            wave_record = {
                "backward_node": w,
                "recomputed": list(step.recompute),
                "mask_replays": list(step.mask_replays),
                "emits_suppressed": 0,
            }

        # 1) rematerialise wave in topological order -----------------------
        for v in step.recompute:
            node = g.nodes[v]
            is_dropout = node.op == ops_mod.DROPOUT
            if is_dropout:
                restore_and_catch_up(dropout_ordinal[v])
            tracker.tick_scratch(scratch_sizes[v])
            out, aux = _eval_forward(
                g, v, {**values, **materialised}, stream,
                emit=False, replay_log=None, replay=is_dropout,
            )
            _require_finite(out, v, "replay")
            materialised[v] = out
            recomputed.append(v)
            recompute_flops += costs[v]
            tracker.add(f"act:{v}", sizes[v])
            if is_dropout:
                held_masks[v] = aux
                tracker.add(f"mask:{v}", sizes[v])
                cursor = dropout_ordinal[v] + 1
            if node.op == ops_mod.EXTERNAL:
                wave_record["emits_suppressed"] += 1
            # free consumed in-wave inputs once their read count hits zero
            for u in node.inputs:
                if g.nodes[u].op in ops_mod.ROOT_OPS:
                    continue
                remaining_reads[u] -= 1
                if remaining_reads[u] == 0:
                    release(u)

        # 2) transient mask-only replay for a retained dropout --------------
        for d in step.mask_replays:
            restore_and_catch_up(dropout_ordinal[d])
            mask = stream.dropout_mask(
                d, g.shapes[d], g.nodes[d].params["p"], replay=True
            )
            held_masks[d] = mask
            recomputed.append(d)
            recompute_flops += costs[d]
            tracker.tick_scratch(scratch_sizes[d])
            cursor = dropout_ordinal[d] + 1

        # 3) VJP ------------------------------------------------------------
        node = g.nodes[w]
        bwd_flops += vjp_costs[w]
        grad_out = grads[w]
        needed_positions = set(ops_mod.needs_input_activations(node.op))
        input_values = [
            act_value(u) if p in needed_positions else None
            for p, u in enumerate(node.inputs)
        ]
        input_shapes = [g.shapes[u] for u in node.inputs]
        forced_mask: Optional[np.ndarray] = None
        if node.op == ops_mod.DROPOUT:
            forced_mask = held_masks.get(w)
            if forced_mask is None:
                raise ComputationError(
                    "dropout VJP ran without a replayed mask",
                    code="E_RNG_MASK_MISSING",
                    context={"node": w, "run_id": run_id},
                )
        vjp_scratch = scratch_sizes[w] + sum(
            sizes[u] for u in node.inputs
        )
        tracker.tick_scratch(vjp_scratch)
        grad_inputs = ops_mod.backward(
            node.op, node.params, input_values, None, grad_out,
            mask_for_dropout=forced_mask, input_shapes=input_shapes,
        )
        if len(grad_inputs) != len(node.inputs):
            raise ComputationError(
                "VJP returned the wrong number of gradients",
                code="E_VJP_ARITY",
                context={"node": w, "expected": len(node.inputs),
                         "got": len(grad_inputs)},
            )
        for u, gu in zip(node.inputs, grad_inputs):
            _require_finite(gu, w, f"backward->{u}")
            unode = g.nodes[u]
            if unode.op == ops_mod.PARAMETER:
                param_grads[u] = (param_grads[u] + gu) if u in param_grads else gu
                tracker.add(f"grad-param:{u}", sizes[u])
            elif unode.op == ops_mod.INPUT:
                input_grads[u] = (input_grads[u] + gu) if u in input_grads else gu
            else:
                grads[u] = grads[u] + gu if u in grads else gu
                tracker.add(f"grad:{u}", sizes[u])
        grads.pop(w, None)
        tracker.drop(f"grad:{w}")
        for u in step.seeds:
            remaining_reads[u] -= 1
            if remaining_reads[u] == 0:
                release(u)
        held_masks.pop(w, None)
        tracker.drop(f"mask:{w}")
        if wave_record is not None:
            replay_log["replay_waves"].append(wave_record)

    replay_log["phases"].append("backward_complete")
    replay_log["measured_peak_memory"] = tracker.peak
    replay_log["emitted_side_effects"] = stream.external_emit_count

    _check_param_gradients(g, param_grads, state, run_id)

    sim = plan.simulation
    expected_recomputed = sorted(
        list(sim.recomputed_nodes) + list(sim.mask_only_nodes)
    )
    if sorted(recomputed) != expected_recomputed:
        raise ComputationError(
            "executor replay set diverged from the planned simulation",
            code="E_REPLAY_DIVERGENCE",
            context={"planned": expected_recomputed,
                     "executed": sorted(recomputed), "run_id": run_id},
        )
    if recompute_flops != sim.recompute_flops:
        raise ComputationError(
            "executor recompute flops diverged from simulation",
            code="E_REPLAY_FLOP_DIVERGENCE",
            context={"planned": sim.recompute_flops,
                     "executed": recompute_flops, "run_id": run_id},
        )
    if tracker.peak != sim.peak_memory:
        raise ComputationError(
            "measured peak memory diverged from simulated peak",
            code="E_MEMORY_DIVERGENCE",
            context={"planned_peak": sim.peak_memory,
                     "measured_peak": tracker.peak, "run_id": run_id},
        )

    run = RunState(
        run_id=run_id,
        status=RunStatus.EXECUTED,
        loss=loss,
        grads=param_grads,
        recomputed_nodes=tuple(recomputed),
        forward_flops=sim.forward_flops,
        recompute_flops=recompute_flops,
        backward_flops=bwd_flops,
        peak_memory=tracker.peak,
        emitted_side_effects=stream.external_emit_count,
        rng_replay_ok=True,
    )
    replay_log["input_grads"] = {
        k: v.tolist() for k, v in sorted(input_grads.items())
    }
    replay_log["loss"] = loss
    return run, replay_log


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _eval_forward(g: ValidatedGraph, nid: str,
                  available: Mapping[str, np.ndarray], stream: RandomStream,
                  *, emit: bool, replay_log: Optional[Dict[str, Any]],
                  replay: bool = False) -> Tuple[np.ndarray, Any]:
    node = g.nodes[nid]
    in_values = [available[u] for u in node.inputs]
    if node.op == ops_mod.DROPOUT and replay and stream.strategy == "counter":
        mask = stream.dropout_mask(
            nid, g.shapes[nid], node.params["p"], replay=True
        )
        out, aux = ops_mod.forward(
            node.op, node.params, in_values, stream, nid, emit=emit,
            forced_mask=mask,
        )
    else:
        out, aux = ops_mod.forward(
            node.op, node.params, in_values, stream, nid, emit=emit
        )
    if node.op == ops_mod.EXTERNAL and emit and replay_log is not None:
        replay_log["external_emits"].append(nid)
    return out, aux


def _prepare_roots(g: ValidatedGraph, state: TrainingState,
                   inputs: Mapping[str, np.ndarray]) -> Dict[str, np.ndarray]:
    roots: Dict[str, np.ndarray] = {}
    for nid in g.order:
        node = g.nodes[nid]
        if node.op == ops_mod.PARAMETER:
            roots[nid] = np.array(state.get(nid), copy=True)
        elif node.op == ops_mod.INPUT:
            if nid not in inputs:
                raise InvalidInputError(
                    f"missing value for input node {nid!r}",
                    code="E_INPUT_MISSING",
                    context={"node": nid},
                )
            arr = np.asarray(inputs[nid], dtype=SUPPORTED_DTYPE)
            if tuple(arr.shape) != g.shapes[nid]:
                raise InvalidInputError(
                    "input shape does not match graph",
                    code="E_INPUT_SHAPE",
                    context={"node": nid, "expected": list(g.shapes[nid]),
                             "got": list(arr.shape)},
                )
            if not np.all(np.isfinite(arr)):
                raise InvalidInputError(
                    f"input {nid!r} contains NaN/Inf",
                    code="E_TENSOR_NONFINITE",
                    context={"node": nid},
                )
            roots[nid] = arr
    return roots


def _cost_tables(g: ValidatedGraph):
    sizes, scratches, costs, vjp_costs = {}, {}, {}, {}
    for nid in g.order:
        sh = g.shapes[nid]
        in_shapes = [g.shapes[r] for r in g.nodes[nid].inputs]
        sizes[nid] = ops_mod.numel(sh)
        scratches[nid] = ops_mod.scratch_cost(g.nodes[nid].op, in_shapes, sh)
        costs[nid] = ops_mod.compute_cost(g.nodes[nid].op, in_shapes, sh)
        vjp_costs[nid] = ops_mod.backward_cost(g.nodes[nid].op, in_shapes, sh)
    return sizes, scratches, costs, vjp_costs


def _require_finite(arr: np.ndarray, nid: str, phase: str) -> None:
    if not np.all(np.isfinite(arr)):
        raise ComputationError(
            f"non-finite value produced at {nid!r} during {phase}",
            code="E_COMP_NONFINITE",
            context={"node": nid, "phase": phase},
        )


def _check_param_gradients(g: ValidatedGraph, param_grads: Dict[str, np.ndarray],
                           state: TrainingState, run_id: str) -> None:
    for pid in state.parameter_ids():
        if pid not in param_grads:
            raise ComputationError(
                f"no gradient produced for parameter {pid!r}",
                code="E_GRAD_MISSING",
                context={"parameter": pid, "run_id": run_id},
            )
        if tuple(param_grads[pid].shape) != tuple(state.shape_of(pid)):
            raise ComputationError(
                "gradient shape mismatch for parameter",
                code="E_GRAD_SHAPE",
                context={"parameter": pid, "run_id": run_id},
            )
