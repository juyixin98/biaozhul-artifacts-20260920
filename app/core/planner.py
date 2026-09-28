"""Checkpoint planning.

Two search strategies share the same :func:`app.core.memory.simulate`
contract as the executor:

* ``exhaustive`` -- enumerate *every* checkpoint subset (small graphs). Used
  by the tests to cross-check peak memory and recompute cost over the whole
  search space and by default for graphs up to :data:`EXHAUSTIVE_LIMIT`.
* ``greedy``     -- start "retain everything", then drop the checkpoint that
  reduces peak memory most while adding the least recomputation (big graphs).

Objective (both): among plans whose simulated peak fits ``memory_budget``,
minimise extra recompute, then peak memory; ties break on the checkpoint set
so the answer is deterministic.  The plan always carries the extra compute
cost explicitly -- a planner never "solves" an over-budget graph by dropping
activations the VJP needs; such choices simply do not exist in the search
space (the simulator closes every replay dependency).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import ops as ops_mod
from .errors import ResourceExhaustedError
from .graph import ValidatedGraph
from .memory import SimulationResult, simulate

EXHAUSTIVE_LIMIT = 14  # 2**14 subsets worst case


@dataclass(frozen=True)
class CheckpointPlan:
    retained: Tuple[str, ...]
    """Checkpointed internal nodes (always includes the target)."""
    simulation: SimulationResult
    search: str
    candidates_evaluated: int
    memory_budget: Optional[int]
    min_achievable_peak: int
    """Smallest peak any subset can achieve (diagnostic when infeasible)."""

    @property
    def extra_compute_flops(self) -> int:
        return self.simulation.recompute_flops

    def summary(self) -> Dict[str, object]:
        s = self.simulation
        return {
            "retained": list(self.retained),
            "search": self.search,
            "candidates_evaluated": self.candidates_evaluated,
            "memory_budget": self.memory_budget,
            "peak_memory": s.peak_memory,
            "min_achievable_peak": self.min_achievable_peak,
            "retained_memory": s.retained_memory,
            "workspace_peak": s.workspace_peak,
            "forward_flops": s.forward_flops,
            "recompute_flops": s.recompute_flops,
            "backward_flops": s.backward_flops,
            "extra_compute_ratio": round(s.extra_compute_ratio, 6),
            "recomputed_nodes": list(s.recomputed_nodes),
            "mask_only_nodes": list(s.mask_only_nodes),
            "replay_waves": [list(w) for w in s.replay_waves],
            "rng_snapshot_memory": s.rng_snapshot_memory,
            "peak_live": list(s.peak_live),
        }


def plan_checkpoints(
    g: ValidatedGraph,
    memory_budget: Optional[int] = None,
    *,
    rng_strategy: str = "counter",
    force_search: Optional[str] = None,
) -> CheckpointPlan:
    """Return the best feasible checkpoint plan or raise resource exhaustion."""

    if memory_budget is not None:
        if not isinstance(memory_budget, int) or isinstance(memory_budget, bool):
            raise TypeError("memory_budget must be an int or None")
        if memory_budget <= 0:
            raise ValueError("memory_budget must be positive")

    internal = [
        nid for nid in g.order
        if g.nodes[nid].op not in ops_mod.ROOT_OPS and nid != g.target
    ]
    search = force_search or (
        "exhaustive" if len(internal) <= EXHAUSTIVE_LIMIT else "greedy"
    )

    if search == "exhaustive":
        retained, sim, min_peak, min_peak_retained, evaluated = _exhaustive(
            g, internal, memory_budget, rng_strategy
        )
    elif search == "greedy":
        retained, sim, min_peak, min_peak_retained, evaluated = _greedy(
            g, internal, memory_budget, rng_strategy
        )
    else:
        raise ValueError(f"unknown search strategy {search!r}")

    if memory_budget is not None and sim.peak_memory > memory_budget:
        raise ResourceExhaustedError(
            "no checkpoint plan satisfies the memory budget",
            code="E_BUDGET_INFEASIBLE",
            context={
                "budget": memory_budget,
                "min_achievable_peak": min_peak,
                "shortfall": min_peak - memory_budget,
                "search": search,
                "candidates_evaluated": evaluated,
                "min_peak_plan_retained": list(min_peak_retained),
            },
        )

    target = g.target
    full_retained = tuple(
        nid for nid in g.order
        if nid == target or (
            nid in set(retained) and g.nodes[nid].op not in ops_mod.ROOT_OPS
        )
    )
    return CheckpointPlan(
        retained=full_retained,
        simulation=sim,
        search=search,
        candidates_evaluated=evaluated,
        memory_budget=memory_budget,
        min_achievable_peak=min_peak,
    )


# --------------------------------------------------------------------------
# Exhaustive search
# --------------------------------------------------------------------------


def _exhaustive(
    g: ValidatedGraph, internal: List[str], budget: Optional[int],
    rng_strategy: str,
) -> Tuple[List[str], SimulationResult, int, List[str], int]:
    best: Optional[SimulationResult] = None
    best_retained: List[str] = []
    best_key = None
    min_peak: Optional[int] = None
    min_peak_retained: List[str] = []
    evaluated = 0
    n = len(internal)
    for mask in range(1 << n):
        retained = [internal[i] for i in range(n) if mask & (1 << i)]
        sim = simulate(g, retained, rng_strategy=rng_strategy)
        evaluated += 1
        if min_peak is None or sim.peak_memory < min_peak:
            min_peak = sim.peak_memory
            min_peak_retained = retained
        if budget is not None and sim.peak_memory > budget:
            continue
        # Fewer recompute flops first, then lower peak, then sparser set /
        # lexicographic order for determinism.
        key = (sim.recompute_flops, sim.peak_memory, len(retained),
               tuple(retained))
        if best is None or key < best_key:
            best, best_key, best_retained = sim, key, retained
    if best is None:
        # Budget infeasible: hand back the plan that achieved min peak so the
        # caller's error context names the closest possible plan.
        best = simulate(g, min_peak_retained, rng_strategy=rng_strategy)
    return (best_retained, best,
            min_peak if min_peak is not None else best.peak_memory,
            min_peak_retained, evaluated)


# --------------------------------------------------------------------------
# Greedy search
# --------------------------------------------------------------------------


def _greedy(
    g: ValidatedGraph, internal: List[str], budget: Optional[int],
    rng_strategy: str,
) -> Tuple[List[str], SimulationResult, int, List[str], int]:
    """Best-improvement local search over checkpoint subsets.

    Lexicographic key: feasibility first (and, while infeasible, lowest
    peak), then recompute flops, then peak.  Dropping a checkpoint never
    needs extra memory for retained storage, so the walk is a descent; once
    feasible it only accepts still-feasible, cheaper/lower-peak neighbours.
    """

    retained = set(internal)  # start: no recompute at all
    current = simulate(g, list(retained), rng_strategy=rng_strategy)
    evaluated = 1
    min_peak = current.peak_memory
    min_peak_retained = list(retained)

    def infeasible(sim: SimulationResult) -> bool:
        return budget is not None and sim.peak_memory > budget

    def key(sim: SimulationResult):
        bad = infeasible(sim)
        # While infeasible, minimise peak aggressively; once feasible pin
        # that component to 0 and minimise recompute, then peak.
        return (int(bad), sim.peak_memory if bad else 0,
                sim.recompute_flops, sim.peak_memory)

    while True:
        choice = None  # (key, sim, dropped_id)
        for cand in sorted(retained):
            trial = simulate(g, list(retained - {cand}), rng_strategy)
            evaluated += 1
            if trial.peak_memory < min_peak:
                min_peak = trial.peak_memory
                min_peak_retained = list(retained - {cand})
            tkey = key(trial)
            if tkey < key(current):
                if choice is None or tkey < choice[0]:
                    choice = (tkey, trial, cand)
        if choice is None:
            break
        current = choice[1]
        retained.discard(choice[2])
    return list(retained), current, min_peak, min_peak_retained, evaluated
