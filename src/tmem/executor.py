"""Execute a planned DAG two ways and compare.

* ``execute_no_reuse`` allocates a fresh NumPy array for every tensor and
  frees nothing until the end: the straightforward, obviously-correct
  baseline (peak = sum of every tensor's storage, alias views included).
* ``execute_reuse`` allocates exactly ONE arena of the planner's size and
  writes every root tensor at its planned flat offset; slice results are
  ordinary NumPy slices of the source (the source already being a view into
  that arena), so they alias the same storage. Any overlap/liveness mistake
  in the plan therefore corrupts later results instead of being hidden.

Inputs are reproducible synthetic data (seeded): each input gets distinct
deterministic values so aliasing errors are detectable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .dag import ADD, MATMUL, SLICE, DAG
from .planner import MemoryPlan, num_elements, plan_memory

DTYPE = np.float32


@dataclass
class ExecutionResult:
    outputs: dict[str, np.ndarray]
    peak_elements: int
    peak_bytes: int
    mode: str
    allocations: int          # number of distinct storage allocations
    timeline: list[dict]


def make_inputs(dag: DAG, seed: int = 143) -> dict[str, np.ndarray]:
    """Deterministic, distinct-valued inputs (no external data)."""
    values: dict[str, np.ndarray] = {}
    for k, name in enumerate(dag.inputs):
        rng = np.random.default_rng(seed + 1000 * (k + 1))
        # Scale kept modest so chained matmuls do not overflow float32.
        values[name] = (
            rng.standard_normal(size=dag.nodes[name].shape, dtype=np.float64)
            .astype(DTYPE)
            / np.sqrt(num_elements(dag.nodes[name].shape) or 1)
        )
    return values


def _run_op(node, args: list[np.ndarray]) -> np.ndarray:
    if node.op == ADD:
        return np.add(args[0], args[1], dtype=DTYPE)
    if node.op == MATMUL:
        return np.matmul(args[0], args[1]).astype(DTYPE, copy=False)
    if node.op == SLICE:
        spec = node.slice_spec
        view = args[0][
            tuple(slice(s, e) for s, e in zip(spec.starts, spec.stops))
        ]
        # Naive baseline semantics: every node owns a fresh, independent
        # buffer, so slice results are materialised instead of aliased.
        return np.ascontiguousarray(view).astype(DTYPE, copy=True)
    raise AssertionError(f"unexpected op {node.op!r}")


# ---------------------------------------------------------------------------
# Baseline: no reuse
# ---------------------------------------------------------------------------

def execute_no_reuse(
    dag: DAG,
    inputs: Optional[dict[str, np.ndarray]] = None,
    seed: int = 143,
) -> ExecutionResult:
    """Naive baseline: one independent allocation per tensor, never freed."""
    inputs = inputs if inputs is not None else make_inputs(dag, seed)
    env: dict[str, np.ndarray] = dict(inputs)
    timeline: list[dict] = []
    total = sum(num_elements(dag.nodes[n].shape) for n in dag.inputs)
    allocations = len(dag.inputs)

    for point, name in enumerate(dag.order):
        node = dag.nodes[name]
        if not node.is_input:
            result = _run_op(node, [env[a] for a in node.inputs])
            env[name] = result
            allocations += 1
            total += num_elements(node.shape)
        timeline.append(
            {
                "point": point,
                "name": name,
                "op": node.op,
                "cumulative_elements": total,
            }
        )

    return ExecutionResult(
        outputs={n: env[n] for n in dag.outputs},
        peak_elements=total,
        peak_bytes=total * DTYPE().itemsize,
        mode="no_reuse",
        allocations=allocations,
        timeline=timeline,
    )


# ---------------------------------------------------------------------------
# Reuse: one arena, planned offsets
# ---------------------------------------------------------------------------

def _slice_view(source: np.ndarray, node) -> np.ndarray:
    """A slice node produces a true view of its source (which itself is
    already a view into the single arena), so chained slices alias storage
    correctly with no hand-computed offsets."""
    spec = node.slice_spec
    return source[tuple(slice(s, e) for s, e in zip(spec.starts, spec.stops))]


def execute_reuse(
    dag: DAG,
    plan: Optional[MemoryPlan] = None,
    inputs: Optional[dict[str, np.ndarray]] = None,
    seed: int = 143,
) -> ExecutionResult:
    plan = plan if plan is not None else plan_memory(dag)
    inputs = inputs if inputs is not None else make_inputs(dag, seed)

    arena = np.zeros(plan.arena_elements, dtype=DTYPE)
    env: dict[str, np.ndarray] = {}
    timeline: list[dict] = []

    # Inputs are copied into their planned windows too: this makes the
    # exercise a pure one-arena model (no hidden external allocations that
    # could mask a planner bug). Inputs live from point 0.
    for point, name in enumerate(dag.order):
        node = dag.nodes[name]
        bid = plan.buffer_of[name]
        buf = plan.buffers[bid]

        if node.is_input:
            view = arena[buf.offset : buf.offset + buf.size].reshape(node.shape)
            np.copyto(view, inputs[name])
            env[name] = view
        elif node.op == SLICE:
            env[name] = _slice_view(env[node.inputs[0]], node)
        else:
            args = [env[a] for a in node.inputs]
            out_view = arena[buf.offset : buf.offset + buf.size].reshape(node.shape)
            result = _run_op(node, args)
            np.copyto(out_view, result)
            # Keep the canonical env handle pointing into the arena.
            env[name] = out_view

        live = plan.live_buffers_at[point]
        peak_now = sum(plan.buffers[b].size for b in live)
        timeline.append(
            {
                "point": point,
                "name": name,
                "op": node.op,
                "arena_offset": None if node.op == SLICE else buf.offset,
                "buffer": bid,
                "live_elements": peak_now,
            }
        )

    return ExecutionResult(
        outputs={n: env[n].copy() for n in dag.outputs},
        peak_elements=plan.arena_elements,
        peak_bytes=plan.arena_bytes,
        mode="reuse",
        allocations=1,
        timeline=timeline,
    )


def execute(
    dag: DAG,
    mode: str = "both",
    seed: int = 143,
    peak_budget_elements: Optional[int] = None,
    enforce_budget: bool = False,
) -> dict:
    """Run ``no_reuse`` / ``reuse`` / ``both`` and compare output values."""
    plan = plan_memory(
        dag,
        peak_budget_elements=peak_budget_elements,
        enforce_budget=enforce_budget,
    )
    inputs = make_inputs(dag, seed)

    results: dict[str, ExecutionResult] = {}
    if mode in ("no_reuse", "both"):
        results["no_reuse"] = execute_no_reuse(dag, inputs=inputs)
    if mode in ("reuse", "both"):
        results["reuse"] = execute_reuse(dag, plan=plan, inputs=inputs)

    comparison = None
    if mode == "both":
        comparison = compare_results(
            results["no_reuse"].outputs, results["reuse"].outputs
        )

    return {
        "mode": mode,
        "plan": plan,
        "results": results,
        "comparison": comparison,
        "inputs": inputs,
    }


def compare_results(
    a: dict[str, np.ndarray], b: dict[str, np.ndarray], rtol: float = 1e-4
) -> dict:
    """Elementwise comparison of the two executors' outputs (float32)."""
    assert set(a) == set(b), (set(a), set(b))
    per_output = {}
    all_match = True
    for name in a:
        x, y = np.broadcast_arrays(a[name], b[name])
        exact = bool(np.array_equal(x, y))
        close = bool(np.allclose(x, y, rtol=rtol, atol=1e-5))
        diff = np.abs(x.astype(np.float64) - y.astype(np.float64))
        per_output[name] = {
            "shape": list(a[name].shape),
            "exact_equal": exact,
            "allclose": close,
            "max_abs_diff": float(diff.max(initial=0.0)),
            "mean_abs_diff": float(diff.mean() if diff.size else 0.0),
        }
        all_match = all_match and close
    return {"match": all_match, "rtol": rtol, "outputs": per_output}
