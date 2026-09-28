"""Forward evaluation of a program with numerical diagnostics.

Responsibilities:
- bind flat input values to input nodes
- detect non-finite intermediate results (overflow / domain errors) and
  report them as computation failures with the exact node and op
- detect non-smooth kink points (abs / relu at zero) and either reject
  them or record them, per request policy
- enforce the time budget cooperatively during the node loop
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .config import Budget
from .errors import computation_failure, input_error, nonsmooth_point, resource_exhausted
from .graph import Node
from .ops import OPS

_TIME_CHECK_INTERVAL = 64  # nodes between wall-clock budget checks


@dataclass(frozen=True)
class EvalOptions:
    nonsmooth_policy: str  # "reject" | "subgradient"
    kink_atol: float


@dataclass
class EvalResult:
    values: list[np.ndarray]
    kinks: list[dict[str, Any]] = field(default_factory=list)
    nodes_evaluated: int = 0
    elapsed_ms: float = 0.0


def evaluate(
    nodes: tuple[Node, ...],
    input_node_ids: tuple[int, ...],
    input_arrays: list[np.ndarray],
    options: EvalOptions,
    budget: Budget,
    run_id: str,
) -> EvalResult:
    values: list[np.ndarray | None] = [None] * len(nodes)
    for nid, arr in zip(input_node_ids, input_arrays):
        values[nid] = arr

    kinks: list[dict[str, Any]] = []
    start = time.perf_counter()

    for i, node in enumerate(nodes):
        if node.op == "input":
            if values[i] is None:
                raise input_error("no value bound for input node", node_id=i, name=node.name)
            continue
        op = OPS[node.op]
        args = [values[j] for j in node.inputs]
        if any(a is None for a in args):  # pragma: no cover - defensive
            raise computation_failure(
                "internal error: unevaluated dependency", run_id=run_id, node_id=i
            )
        with np.errstate(all="ignore"):
            out = op.forward(args, node.params)  # type: ignore[arg-type]
        out = np.asarray(out, dtype=np.float64)
        if not bool(np.all(np.isfinite(out))):
            bad = np.argwhere(~np.isfinite(out.reshape(-1)))
            raise computation_failure(
                f"non-finite result at node {i} (op '{node.op}'); "
                "likely overflow or a domain violation such as log of a non-positive value",
                run_id=run_id,
                node_id=i,
                op=node.op,
                inputs=list(node.inputs),
                first_bad_flat_index=int(bad[0][0]) if len(bad) else None,
                non_finite_count=int(len(bad)),
            )
        if op.kink_fn is not None:
            hits = op.kink_fn(args, node.params, options.kink_atol)
            if hits:
                if options.nonsmooth_policy == "reject":
                    raise nonsmooth_point(
                        f"non-smooth point hit at node {i} (op '{node.op}'); "
                        "retry with nonsmooth_policy='subgradient' and an explicit "
                        "subgradient value, or perturb the point",
                        run_id=run_id,
                        node_id=i,
                        op=node.op,
                        kinks=hits,
                        kink_atol=options.kink_atol,
                    )
                kinks.append({"node_id": i, "op": node.op, "kinks": hits})
        values[i] = out

        if budget.time_budget_ms is not None and i % _TIME_CHECK_INTERVAL == 0:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if elapsed_ms > budget.time_budget_ms:
                raise resource_exhausted(
                    "time budget exceeded during evaluation",
                    run_id=run_id,
                    nodes_evaluated=i + 1,
                    elapsed_ms=round(elapsed_ms, 3),
                    time_budget_ms=budget.time_budget_ms,
                )

    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return EvalResult(
        values=values,  # type: ignore[arg-type]
        kinks=kinks,
        nodes_evaluated=len(nodes),
        elapsed_ms=elapsed_ms,
    )
