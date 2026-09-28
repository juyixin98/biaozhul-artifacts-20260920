"""Exact memory/cost simulator for a checkpoint choice.

The simulator is the single accounting contract shared by the planner (which
searches checkpoint choices) and the executor (which follows the chosen
plan): both consume the discrete-event backward schedule produced by
:func:`build_backward_schedule` and account the same buffer lifetimes.

Replay / reference-counting model
----------------------------------
* The forward pass frees a non-checkpointed activation as soon as its last
  forward consumer has run; checkpointed activations survive into backward.
* Backward proceeds in reverse topological order.  Before differentiating
  node ``w``, every *seed* activation its VJP reads is made available:
  retained checkpoints and roots are present, and every missing seed is
  rematerialised by replaying the ancestor sub-DAG up to the nearest
  retained/root boundary.
* Inside one replay wave the sub-DAG is evaluated in topological order and
  an intermediate is freed the instant its last in-wave consumer read it, so
  a wave only holds a small frontier rather than the whole closure.
* A rematerialised node that still has readers in *later* waves is held with
  a reference count and is **never computed twice**: every activation gets
  exactly one production event (checkpoint or rematerialisation), and a
  per-activation read counter drives its free.  This is the shared-subgraph
  refcount rule.
* A dropout node's VJP needs its stochastic mask at the dropout's own
  backward step; the mask is always regenerated there as a mask-only replay
  (RNG replay, no ancestor work, no extra output buffer).

Costs are counted in float64 **elements**.  Buffer classes:

* persistent roots (inputs / parameters),
* retained vs rematerialised activations,
* one gradient accumulator per internal node / parameter,
* workspace: op scratch plus short-lived VJP contribution buffers,
* one 624-word global RNG snapshot when ``strategy="snapshot"`` is used on a
  graph containing a stochastic op.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Sequence, Set, Tuple

from . import ops as ops_mod
from .graph import ValidatedGraph

# numpy RandomState(MT19937) state = 624 uint32 words + position pointer.
RNG_STATE_ELEMENTS = 624


@dataclass(frozen=True)
class SimulationResult:
    peak_memory: int
    forward_flops: int
    recompute_flops: int
    backward_flops: int
    retained_memory: int
    workspace_peak: int
    recomputed_nodes: Tuple[str, ...]
    mask_only_nodes: Tuple[str, ...]
    replay_waves: Tuple[Tuple[str, ...], ...]
    rng_snapshot_memory: int
    peak_live: Tuple[str, ...]
    feasible: bool = True

    @property
    def extra_compute_ratio(self) -> float:
        if self.forward_flops == 0:
            return 0.0
        return self.recompute_flops / self.forward_flops


@dataclass(frozen=True)
class ReplayStep:
    """One backward step's executable replay/VJP schedule."""

    node: str
    recompute: Tuple[str, ...]
    """Non-root nodes produced in this wave, topological order."""
    mask_replays: Tuple[str, ...]
    """Retained dropout nodes whose mask is redrawn transiently this step."""
    seeds: Tuple[str, ...]
    """Non-root input activations this VJP reads (after replay)."""


@dataclass(frozen=True)
class BackwardSchedule:
    steps: Tuple[ReplayStep, ...]
    retained: FrozenSet[str]
    rematerialised: FrozenSet[str]
    """Every node produced by replay (exactly once)."""
    reads: Dict[str, int]
    """Total backward read events for each non-root activation."""
    mask_held_from: Dict[str, int]
    """Rematerialised dropout id -> wave step that produces its held mask."""


# --------------------------------------------------------------------------
# Graph analysis
# --------------------------------------------------------------------------


@dataclass
class _GraphInfo:
    nonroot: List[str]
    rev: List[str]
    step: Dict[str, int]
    size: Dict[str, int]
    scratch: Dict[str, int]
    cost: Dict[str, int]
    vjp_cost: Dict[str, int]
    is_param: Dict[str, bool]
    is_input: Dict[str, bool]


def _analyse(g: ValidatedGraph) -> _GraphInfo:
    nonroot = [nid for nid in g.order if g.nodes[nid].op not in ops_mod.ROOT_OPS]
    rev = list(reversed(nonroot))
    step = {nid: i for i, nid in enumerate(rev)}
    size, scratch, cost, vjp_cost = {}, {}, {}, {}
    is_param, is_input = {}, {}
    for nid in g.order:
        n = g.nodes[nid]
        sh = g.shapes[nid]
        in_shapes = [g.shapes[r] for r in n.inputs]
        size[nid] = ops_mod.numel(sh)
        scratch[nid] = ops_mod.scratch_cost(n.op, in_shapes, sh)
        cost[nid] = ops_mod.compute_cost(n.op, in_shapes, sh)
        vjp_cost[nid] = ops_mod.backward_cost(n.op, in_shapes, sh)
        is_param[nid] = n.op == ops_mod.PARAMETER
        is_input[nid] = n.op == ops_mod.INPUT
    return _GraphInfo(
        nonroot=nonroot, rev=rev, step=step, size=size, scratch=scratch,
        cost=cost, vjp_cost=vjp_cost, is_param=is_param, is_input=is_input,
    )


def _seeds(g: ValidatedGraph, w: str) -> Tuple[str, ...]:
    """Non-root input activations that w's VJP must read."""

    positions = ops_mod.needs_input_activations(g.nodes[w].op)
    return tuple(
        g.nodes[w].inputs[p]
        for p in positions
        if g.nodes[g.nodes[w].inputs[p]].op not in ops_mod.ROOT_OPS
    )


def _ancestor_closure(g: ValidatedGraph, retained: FrozenSet[str],
                      seeds: Sequence[str]) -> FrozenSet[str]:
    """All non-retained, non-root ancestors required to produce the seeds."""

    seen: Set[str] = set()
    stack = [s for s in seeds if s not in retained]
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        for u in g.nodes[x].inputs:
            if u in retained or g.nodes[u].op in ops_mod.ROOT_OPS:
                continue
            stack.append(u)
    return frozenset(seen)


# --------------------------------------------------------------------------
# Backward schedule (shared by simulator and executor)
# --------------------------------------------------------------------------


def build_backward_schedule(g: ValidatedGraph,
                            retained: Sequence[str]) -> BackwardSchedule:
    R = frozenset(retained) | {g.target}
    _validate_retained(g, set(R))
    info = _analyse(g)
    rank = {nid: i for i, nid in enumerate(g.order)}

    raw_closures: List[FrozenSet[str]] = []
    steps: List[ReplayStep] = []
    for w in info.rev:
        seeds = _seeds(g, w)
        closure = _ancestor_closure(g, R, seeds)
        raw_closures.append(closure)
        masks = (w,) if g.nodes[w].op == ops_mod.DROPOUT else ()
        steps.append(ReplayStep(
            node=w, recompute=(), mask_replays=masks, seeds=seeds
        ))

    # A node is produced in the earliest wave whose closure contains it.
    first_step: Dict[str, int] = {}
    for s, closure in enumerate(raw_closures):
        for v in closure:
            if v not in first_step:
                first_step[v] = s
    produced: Dict[int, List[str]] = {}
    for v, s in first_step.items():
        produced.setdefault(s, []).append(v)

    reads: Dict[str, int] = {nid: 0 for nid in info.nonroot}
    mask_held_from: Dict[str, int] = {}
    final_steps: List[ReplayStep] = []
    for s, w in enumerate(info.rev):
        wave = sorted(produced.get(s, ()), key=lambda nid: rank[nid])
        # Replay-time reads: every produced node numerically reads ALL of
        # its non-root inputs (retained boundaries, earlier-held remats or
        # transient values produced in this wave).
        for v in wave:
            for u in g.nodes[v].inputs:
                if g.nodes[u].op not in ops_mod.ROOT_OPS:
                    reads[u] += 1
            if g.nodes[v].op == ops_mod.DROPOUT:
                # The mask drawn during this replay must survive to the
                # dropout's own (later) VJP step.
                mask_held_from[v] = s
        # VJP seed reads.
        for u in steps[s].seeds:
            reads[u] += 1
        # A dropout whose output is never rematerialised still needs its
        # mask at its own VJP: draw it transiently in this very step.
        transient_masks = tuple(
            d for d in steps[s].mask_replays
            if d not in first_step
        )
        final_steps.append(ReplayStep(
            node=w,
            recompute=tuple(wave),
            mask_replays=transient_masks,
            seeds=steps[s].seeds,
        ))

    return BackwardSchedule(
        steps=tuple(final_steps),
        retained=R,
        rematerialised=frozenset(first_step),
        reads=reads,
        mask_held_from=mask_held_from,
    )


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------


def simulate(g: ValidatedGraph, retained: Sequence[str],
             rng_strategy: str = "counter") -> SimulationResult:
    """Run the exact accounting simulation for one checkpoint choice."""

    info = _analyse(g)
    schedule = build_backward_schedule(g, retained)
    R = schedule.retained

    live: Dict[str, int] = {}
    peak = 0
    peak_live: Tuple[str, ...] = ()
    workspace_peak = 0

    def account(scratch_extra: int = 0) -> None:
        nonlocal peak, peak_live, workspace_peak
        total = sum(live.values()) + scratch_extra
        if total > peak:
            peak = total
            peak_live = tuple(sorted(live))
        if scratch_extra > workspace_peak:
            workspace_peak = scratch_extra

    # -- persistent roots -------------------------------------------------
    for nid in g.order:
        if g.nodes[nid].op in ops_mod.ROOT_OPS:
            live[f"root:{nid}"] = info.size[nid]
    has_stochastic = any(
        ops_mod.SPECS[g.nodes[nid].op].stochastic for nid in g.order
    )
    if rng_strategy == "snapshot" and has_stochastic:
        live["rng:snapshot"] = RNG_STATE_ELEMENTS
    account()

    # -- forward -----------------------------------------------------------
    fwd_refs = dict(g.user_count)
    fwd_flops = 0
    for nid in g.order:
        n = g.nodes[nid]
        if n.op in ops_mod.ROOT_OPS:
            continue
        fwd_flops += info.cost[nid]
        account(info.scratch[nid])
        live[f"act:{nid}"] = info.size[nid]
        account()
        for u in n.inputs:
            if g.nodes[u].op in ops_mod.ROOT_OPS:
                continue
            fwd_refs[u] -= 1
            if fwd_refs[u] == 0 and u not in R:
                live.pop(f"act:{u}", None)
        account()

    retained_memory = sum(
        info.size[nid] for nid in R if g.nodes[nid].op not in ops_mod.ROOT_OPS
    )
    rng_memory = (
        RNG_STATE_ELEMENTS
        if rng_strategy == "snapshot" and has_stochastic else 0
    )

    # -- backward ----------------------------------------------------------
    remaining_reads = dict(schedule.reads)
    recompute_flops = 0
    bwd_flops = 0
    recomputed: List[str] = []
    mask_only: List[str] = []
    waves: List[Tuple[str, ...]] = []

    target = g.target
    live[f"grad:{target}"] = info.size[target]
    # The target activation itself has no backward readers; release it now.
    if remaining_reads.get(target, 0) == 0:
        live.pop(f"act:{target}", None)
    account()

    def release(x: str) -> None:
        live.pop(f"act:{x}", None)

    for s, step in enumerate(schedule.steps):
        w = step.node
        wave_label: List[str] = []
        # 1) rematerialise in topological order ----------------------------
        for v in step.recompute:
            account(info.scratch[v])
            recompute_flops += info.cost[v]
            recomputed.append(v)
            wave_label.append(v)
            live[f"act:{v}"] = info.size[v]
            if g.nodes[v].op == ops_mod.DROPOUT:
                # The drawn mask is held (output-sized) until this dropout's
                # own VJP; the draw is already included in cost[v].
                live[f"mask:{v}"] = info.size[v]
            account()
            for u in g.nodes[v].inputs:
                if g.nodes[u].op in ops_mod.ROOT_OPS:
                    continue
                remaining_reads[u] -= 1
                if remaining_reads[u] == 0:
                    release(u)
            account()
        # 2) mask-only replays for this VJP --------------------------------
        for d in step.mask_replays:
            mask_only.append(d)
            recompute_flops += info.cost[d]
            wave_label.append(f"{d}#mask")
            account(info.scratch[d])
        # 3) VJP ------------------------------------------------------------
        vjp_scratch = info.scratch[w] + sum(
            info.size[u] for u in g.nodes[w].inputs
        )
        account(vjp_scratch)
        bwd_flops += info.vjp_cost[w]
        for u in g.nodes[w].inputs:
            if info.is_param[u]:
                live.setdefault(f"grad-param:{u}", info.size[u])
            elif info.is_input[u]:
                pass  # data-input gradients are reported, not retained
            else:
                live.setdefault(f"grad:{u}", info.size[u])
        live.pop(f"grad:{w}", None)
        # Seed reads complete at this VJP.
        for u in step.seeds:
            remaining_reads[u] -= 1
            if remaining_reads[u] == 0:
                release(u)
        # This node's held mask (if any) is consumed by its own VJP.
        live.pop(f"mask:{w}", None)
        account()
        if wave_label:
            waves.append(tuple(wave_label))

    return SimulationResult(
        peak_memory=peak,
        forward_flops=fwd_flops,
        recompute_flops=recompute_flops,
        backward_flops=bwd_flops,
        retained_memory=retained_memory,
        workspace_peak=max(workspace_peak,
                           max(info.scratch.values(), default=0)),
        recomputed_nodes=tuple(dict.fromkeys(recomputed)),
        mask_only_nodes=tuple(dict.fromkeys(mask_only)),
        replay_waves=tuple(waves),
        rng_snapshot_memory=rng_memory,
        peak_live=peak_live,
    )


def _validate_retained(g: ValidatedGraph, retained: Set[str]) -> None:
    for nid in retained:
        if nid not in g.nodes:
            raise KeyError(f"retained references unknown node {nid!r}")
        if g.nodes[nid].op in ops_mod.ROOT_OPS:
            raise ValueError(f"root node {nid!r} is always available")
