"""Service layer: orchestrates graph registration and HVP evaluation.

Sits between the HTTP layer and the core modules, owns the run_id, and
emits structured log events with the key intermediate states (program
sizes, kinks, timings, decision reasons) so any run can be replayed.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np

from .autodiff import build_hvp_program
from .config import ServiceConfig, load_config
from .errors import input_error
from .evaluate import EvalOptions, evaluate
from .graph import Graph, build_graph
from .logging_config import log_event
from .state import GraphStore
from .validation import (
    check_eval_budget,
    check_graph_budget,
    resolve_budget,
    validate_flat_vector,
    validate_subgradient,
)


@dataclass(frozen=True)
class HvpRequestData:
    vector: list[float]
    point: list[float] | None = None
    use_stored_point: bool = False
    expected_version: int | None = None
    nonsmooth_policy: str = "reject"
    subgradient: float = 0.0
    kink_atol: float = 0.0
    max_eval_nodes: int | None = None
    time_budget_ms: float | None = None


@dataclass(frozen=True)
class HvpResult:
    run_id: str
    graph_id: str
    value: float
    gradient: list[float]
    hvp: list[float]
    diagnostics: dict[str, Any]


class HvpService:
    def __init__(self, config: ServiceConfig | None = None, store: GraphStore | None = None) -> None:
        self.config = config or load_config()
        self.store = store or GraphStore()

    # -- graph lifecycle ----------------------------------------------------

    def create_graph(self, spec_nodes: list[dict[str, Any]], output: int) -> tuple[str, Graph]:
        graph = build_graph(spec_nodes, output)
        check_graph_budget(graph.node_count, self.config)
        graph_id = self.store.create(graph)
        log_event(
            logging.INFO,
            "graph_created",
            graph_id=graph_id,
            node_count=graph.node_count,
            layout=graph.layout.to_dict(),
        )
        return graph_id, graph

    def get_graph(self, graph_id: str) -> Graph:
        return self.store.get(graph_id).graph

    def delete_graph(self, graph_id: str) -> None:
        self.store.delete(graph_id)
        log_event(logging.INFO, "graph_deleted", graph_id=graph_id)

    def set_point(self, graph_id: str, point: list[float]) -> int:
        graph = self.get_graph(graph_id)
        arr = validate_flat_vector(graph.layout, point, "point")
        version = self.store.set_point(graph_id, arr)
        log_event(logging.INFO, "point_updated", graph_id=graph_id, point_version=version)
        return version

    # -- HVP ----------------------------------------------------------------

    def hvp(self, graph_id: str, req: HvpRequestData) -> HvpResult:
        run_id = uuid.uuid4().hex[:12]
        stored = self.store.get(graph_id)
        graph = stored.graph

        if req.use_stored_point:
            point, point_version = self.store.stored_point(graph_id, req.expected_version)
        else:
            if req.point is None:
                raise input_error(
                    "either 'point' or use_stored_point=true is required",
                    graph_id=graph_id,
                )
            point = validate_flat_vector(graph.layout, req.point, "point")
            point_version = stored.point_version
        vector = validate_flat_vector(graph.layout, req.vector, "vector")

        if req.nonsmooth_policy not in ("reject", "subgradient"):
            raise input_error(
                "nonsmooth_policy must be 'reject' or 'subgradient'",
                nonsmooth_policy=req.nonsmooth_policy,
            )
        subgradient = validate_subgradient(req.subgradient)
        if req.kink_atol < 0:
            raise input_error("kink_atol must be non-negative", kink_atol=req.kink_atol)

        budget = resolve_budget(self.config, req.max_eval_nodes, req.time_budget_ms)
        log_event(
            logging.INFO,
            "hvp_start",
            run_id=run_id,
            graph_id=graph_id,
            layout_size=graph.layout.size,
            point_version=point_version,
            nonsmooth_policy=req.nonsmooth_policy,
            subgradient=subgradient,
            budget={"max_eval_nodes": budget.max_eval_nodes, "time_budget_ms": budget.time_budget_ms},
        )

        program = build_hvp_program(graph, vector, subgradient)
        check_eval_budget(program.total_nodes, budget, run_id)
        log_event(
            logging.INFO,
            "hvp_program_built",
            run_id=run_id,
            graph_id=graph_id,
            forward_nodes=program.forward_nodes,
            gradient_nodes=program.gradient_nodes,
            total_nodes=program.total_nodes,
        )

        input_arrays = [point[s.offset : s.offset + s.size].reshape(s.shape) for s in graph.layout.slots]
        result = evaluate(
            program.nodes,
            program.input_node_ids,
            input_arrays,
            EvalOptions(nonsmooth_policy=req.nonsmooth_policy, kink_atol=req.kink_atol),
            budget,
            run_id,
        )

        value = float(result.values[program.value_id])
        gradient = np.concatenate([result.values[g].reshape(-1) for g in program.grad_ids])
        hvp = np.concatenate([result.values[h].reshape(-1) for h in program.hvp_ids])

        diagnostics: dict[str, Any] = {
            "run_id": run_id,
            "graph_id": graph_id,
            "point_version": point_version,
            "forward_nodes": program.forward_nodes,
            "gradient_nodes": program.gradient_nodes,
            "total_nodes": program.total_nodes,
            "nodes_evaluated": result.nodes_evaluated,
            "elapsed_ms": round(result.elapsed_ms, 3),
            "kinks": result.kinks,
            "nonsmooth_policy": req.nonsmooth_policy,
            "subgradient": subgradient,
            "budget": {
                "max_eval_nodes": budget.max_eval_nodes,
                "time_budget_ms": budget.time_budget_ms,
            },
        }
        log_event(
            logging.INFO,
            "hvp_done",
            run_id=run_id,
            graph_id=graph_id,
            nodes_evaluated=result.nodes_evaluated,
            elapsed_ms=round(result.elapsed_ms, 3),
            kink_count=len(result.kinks),
            value=value,
        )
        return HvpResult(
            run_id=run_id,
            graph_id=graph_id,
            value=value,
            gradient=gradient.tolist(),
            hvp=hvp.tolist(),
            diagnostics=diagnostics,
        )
