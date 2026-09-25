"""Local service layer: turn a JSON-style request into a planning report.

Request schema (see examples/*.json)::

    {
      "inputs":  {<name>: [<dim>, ...], ...},
      "ops": [
        {"name": ..., "op": "add"|"matmul", "inputs": [...]},
        {"name": ..., "op": "slice", "inputs": [<src>],
         "starts": [...], "stops": [...]}
      ],
      "outputs": [...],
      "seed": 143,                         # optional, synthetic data seed
      "peak_budget_elements": 1000,        # optional
      "enforce_budget": false              # optional, true -> hard error
    }

The report is fully JSON-serialisable and contains the DAG, the memory plan
(buffers, lifetimes, views, allocation timeline), both executors' peaks and
the numerical equivalence comparison.
"""

from __future__ import annotations

from typing import Any

from .dag import DAG, DAGValidationError, build_dag
from .executor import execute
from .planner import AllocationError, BudgetExceeded, MemoryPlan, overlapping_live_pairs


def _dag_summary(dag: DAG) -> dict[str, Any]:
    return {
        "inputs": [
            {"name": n, "shape": list(dag.nodes[n].shape)} for n in dag.inputs
        ],
        "ops": [
            {
                "point": i,
                "name": n,
                "op": dag.nodes[n].op,
                "inputs": list(dag.nodes[n].inputs),
                "shape": list(dag.nodes[n].shape),
                "is_view": dag.nodes[n].op == "slice",
                "is_output": dag.nodes[n].is_output,
            }
            for i, n in enumerate(dag.order)
            if not dag.nodes[n].is_input
        ],
        "outputs": list(dag.outputs),
        "num_tensors": len(dag.order),
    }


def _plan_summary(plan: MemoryPlan) -> dict[str, Any]:
    buffers = sorted(plan.buffers.values(), key=lambda b: b.id)
    return {
        "arena": {
            "elements": plan.arena_elements,
            "bytes": plan.arena_bytes,
            "dtype": f"float{8 * plan.dtype_itemsize}",
        },
        "theoretical_peak": {
            "elements": plan.theoretical_peak_elements,
            "bytes": plan.theoretical_peak_bytes,
        },
        "buffers": [
            {
                "id": b.id,
                "root": b.root,
                "size": b.size,
                "capacity": b.capacity,
                "offset": b.offset,
                "window": [b.offset, b.offset + b.size],
                "birth": b.birth,
                "last_use": b.last_use,
                "alias_family": list(b.members),
            }
            for b in buffers
        ],
        "views": [
            {
                "name": v.name,
                "root": v.root,
                "relative_window": [v.min_offset, v.end_offset],
                "absolute_window": [
                    plan.buffer_for(v.root).offset + v.min_offset,
                    plan.buffer_for(v.root).offset + v.end_offset,
                ],
                "shape": list(v.shape),
                "strides": list(v.strides),
            }
            for v in plan.views.values()
        ],
        "last_use": plan.last_use,
        "timeline": plan.events,
        "live_buffers_per_point": plan.live_buffers_at,
    }


def _safety_summary(plan: MemoryPlan) -> dict[str, Any]:
    violations = overlapping_live_pairs(plan)
    return {
        "simultaneously_live_windows_disjoint": not violations,
        "overlapping_live_pairs": [list(p) for p in violations],
    }


def _budget_summary(
    plan: MemoryPlan, budget: int | None, enforced: bool
) -> dict[str, Any] | None:
    if budget is None:
        return None
    return {
        "budget_elements": budget,
        "budget_bytes": budget * plan.dtype_itemsize,
        "arena_elements": plan.arena_elements,
        "within_budget": plan.arena_elements <= budget,
        "headroom_elements": budget - plan.arena_elements,
        "enforced": enforced,
    }


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """Validate, plan, execute both modes and assemble the JSON report."""
    if not isinstance(request, dict):
        raise DAGValidationError("request must be a JSON object")

    seed = request.get("seed", 143)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise DAGValidationError("'seed' must be an integer")
    budget = request.get("peak_budget_elements")
    if budget is not None:
        if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
            raise DAGValidationError("peak_budget_elements must be a positive int")
    enforced = bool(request.get("enforce_budget", False))

    dag = build_dag(request)
    run = execute(
        dag,
        mode="both",
        seed=seed,
        peak_budget_elements=budget,
        enforce_budget=enforced,
    )
    plan: MemoryPlan = run["plan"]
    base, reused = run["results"]["no_reuse"], run["results"]["reuse"]

    savings_elements = base.peak_elements - reused.peak_elements
    report = {
        "status": "ok",
        "dag": _dag_summary(dag),
        "plan": _plan_summary(plan),
        "safety": _safety_summary(plan),
        "budget": _budget_summary(plan, budget, enforced),
        "execution": {
            "no_reuse": {
                "peak_elements": base.peak_elements,
                "peak_bytes": base.peak_bytes,
                "allocations": base.allocations,
            },
            "reuse": {
                "peak_elements": reused.peak_elements,
                "peak_bytes": reused.peak_bytes,
                "allocations": reused.allocations,
            },
            "saved_elements": savings_elements,
            "saved_bytes": savings_elements * plan.dtype_itemsize,
            "reduction_pct": round(
                100.0 * savings_elements / base.peak_elements, 3
            )
            if base.peak_elements
            else 0.0,
        },
        "comparison": run["comparison"],
    }
    return report


__all__ = [
    "handle_request",
    "DAGValidationError",
    "AllocationError",
    "BudgetExceeded",
]
