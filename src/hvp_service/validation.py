"""Numerical validation: vector/layout binding and budget resolution.

These checks form the data contract between the API layer and the core:
anything that violates them is an ``input_validation`` or
``resource_exhausted`` error with the observed vs. expected values.
"""

from __future__ import annotations

import numpy as np

from .config import Budget, ServiceConfig
from .errors import input_error, resource_exhausted
from .tensor import Layout


def validate_flat_vector(layout: Layout, raw: object, field: str) -> np.ndarray:
    """Bind a client-supplied flat vector to the input layout."""
    try:
        arr = np.asarray(raw, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise input_error(f"'{field}' must be a list of numbers", error=str(exc)) from exc
    if arr.ndim != 1:
        raise input_error(f"'{field}' must be a flat 1-D list", ndim=int(arr.ndim))
    if arr.size != layout.size:
        raise input_error(
            f"'{field}' length does not match the graph input layout",
            expected_size=layout.size,
            actual_size=int(arr.size),
            layout=layout.to_dict(),
        )
    if not bool(np.all(np.isfinite(arr))):
        raise input_error(f"'{field}' must contain only finite values", field=field)
    return arr


def validate_subgradient(value: float) -> float:
    g0 = float(value)
    if not np.isfinite(g0) or not (-1.0 <= g0 <= 1.0):
        raise input_error(
            "subgradient must be finite and within [-1, 1] "
            "(valid for both abs and relu kinks)",
            subgradient=value,
        )
    return g0


def resolve_budget(
    config: ServiceConfig,
    req_max_eval_nodes: int | None,
    req_time_budget_ms: float | None,
) -> Budget:
    """Requests may only tighten server caps, never raise them."""
    max_nodes = config.max_eval_nodes
    if req_max_eval_nodes is not None:
        if req_max_eval_nodes <= 0:
            raise input_error("max_eval_nodes must be positive", max_eval_nodes=req_max_eval_nodes)
        max_nodes = min(max_nodes, int(req_max_eval_nodes))
    time_budget = config.default_time_budget_ms
    if req_time_budget_ms is not None:
        if req_time_budget_ms <= 0:
            raise input_error("time_budget_ms must be positive", time_budget_ms=req_time_budget_ms)
        time_budget = float(req_time_budget_ms) if time_budget is None else min(time_budget, float(req_time_budget_ms))
    return Budget(max_eval_nodes=max_nodes, time_budget_ms=time_budget)


def check_graph_budget(node_count: int, config: ServiceConfig) -> None:
    if node_count > config.max_graph_nodes:
        raise resource_exhausted(
            "graph exceeds the server node budget",
            node_count=node_count,
            max_graph_nodes=config.max_graph_nodes,
        )


def check_eval_budget(node_count: int, budget: Budget, run_id: str) -> None:
    if node_count > budget.max_eval_nodes:
        raise resource_exhausted(
            "combined value/gradient/HVP program exceeds the evaluation node budget",
            run_id=run_id,
            program_nodes=node_count,
            max_eval_nodes=budget.max_eval_nodes,
        )
