"""Application service: orchestrate planning, execution, reference comparison,
dynamic-shape replanning and run logging behind one boundary.

The HTTP layer and the test scripts talk only to this module; it owns the
registry of live sessions (pinned outputs) and translates nothing about the
error taxonomy — categories from :mod:`tenmem.errors` propagate unchanged.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import numpy as np

from .errors import ReplanningRequiredError, StateConflictError
from .executor import Session, execute_no_reuse
from .graph import Graph, Node, validate_and_schedule
from .ops import get_op
from .planner.memory import Plan, plan_memory
from .runlog import RunLogger
from .tensor import TensorSpec
from .verification import compare_outputs


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    graph: Graph
    plan: Plan
    session: Session
    outputs: dict[str, np.ndarray]
    reference_outputs: dict[str, np.ndarray]
    numerically_equivalent: bool
    max_abs_diff: float
    replanned: bool
    replan_reason: dict | None
    no_reuse_peak_bytes: int


def infer_concrete_graph(graph: Graph, feeds: dict[str, np.ndarray]) -> Graph:
    """Derive a *static* graph whose output specs match the concrete run.

    Used after a ``replanning_required`` event: run shape inference with the
    actual shapes and freeze every node output spec at its observed extent.
    Dynamic extents arrive as int64 scalar graph inputs (as in the
    ``shape_mutate`` fixture), so scalar values resolve from ``feeds``.
    """
    validate_and_schedule(graph)
    shapes: dict[str, tuple[int, ...]] = {
        name: tuple(arr.shape) for name, arr in feeds.items()
    }
    scalars: dict[str, int] = {
        name: int(np.asarray(arr).item())
        for name, arr in feeds.items()
        if arr.dtype == np.int64 and arr.ndim == 0
    }
    rebuilt: list[Node] = []
    for node in graph.nodes:
        op = get_op(node.op)
        in_shapes = [shapes[i] for i in node.inputs]
        scalar_values = tuple(scalars.get(i) for i in node.inputs)
        cfg = dict(node.config)
        cfg["runtime_scalar_values"] = scalar_values
        out_shapes = op.shape_fn(in_shapes, cfg, node.outputs)
        specs = tuple(
            TensorSpec(old.name, tuple(int(x) for x in shape), old.dtype)
            for old, shape in zip(node.outputs, out_shapes)
        )
        for spec in specs:
            shapes[spec.name] = spec.max_shape
        rebuilt.append(Node(node.id, node.op, node.inputs, specs, node.config, node.aliases))
    return Graph(
        name=graph.name,
        inputs=graph.inputs,
        nodes=tuple(rebuilt),
        output_names=graph.output_names,
        constants=graph.constants,
    )


class Engine:
    def __init__(self, logger: RunLogger | None = None) -> None:
        self.logger = logger or RunLogger()
        self._sessions: dict[str, Session] = {}

    def make_plan(
        self, graph: Graph, *, alignment: int = 64, max_bytes: int | None = None
    ) -> Plan:
        return plan_memory(graph, alignment=alignment, max_bytes=max_bytes)

    def run(
        self,
        graph: Graph,
        feeds: dict[str, np.ndarray],
        *,
        constants: dict[str, np.ndarray] | None = None,
        parallel: bool = False,
        alignment: int = 64,
        max_bytes: int | None = None,
        run_id: str | None = None,
        compare_reference: bool = True,
    ) -> RunOutcome:
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        plan = self.make_plan(graph, alignment=alignment, max_bytes=max_bytes)
        self.logger.log_inputs(
            run_id, graph.name, "reuse", feeds, constants, parallel, _plan_brief(plan)
        )

        replanned = False
        replan_reason: dict | None = None
        try:
            session = Session(graph, plan, parallel=parallel, run_id=run_id).run(
                feeds, constants
            )
        except ReplanningRequiredError as exc:
            # Dynamic growth: derive observed shapes, replan to those bounds and
            # re-run. The old undersized plan/buffers are discarded — never
            # reused past capacity.
            replanned = True
            replan_reason = exc.to_dict()
            self.logger.log_failure(
                run_id, exc.category, exc.message, exc.details, stage="initial_execution"
            )
            graph = infer_concrete_graph(graph, feeds)
            plan = self.make_plan(graph, alignment=alignment, max_bytes=max_bytes)
            self.logger.event(run_id, "replan", reason=replan_reason, new_plan=_plan_brief(plan))
            session = Session(graph, plan, parallel=parallel, run_id=run_id).run(
                feeds, constants
            )

        self.logger.log_trace(session.trace)

        # Independent no-reuse execution for numerical equivalence.
        ref_outputs, ref_trace = execute_no_reuse(graph, feeds, constants, alignment=alignment)
        self.logger.log_trace(ref_trace)

        got_outputs = {n: session.output(n) for n in graph.output_names}
        verdict = compare_outputs(got_outputs, ref_outputs)

        self._sessions[run_id] = session
        self.logger.event(
            run_id,
            "verdict",
            numerically_equivalent=verdict.equivalent,
            max_abs_diff=verdict.max_abs_diff,
            replanned=replanned,
            reuse_peak_bytes=plan.peak_bytes,
            no_reuse_peak_bytes=ref_trace.peak_capacity_bytes,
            tensor_verdicts=verdict.to_dict()["tensors"],
        )
        return RunOutcome(
            run_id=run_id,
            graph=graph,
            plan=plan,
            session=session,
            outputs=got_outputs,
            reference_outputs=ref_outputs,
            numerically_equivalent=verdict.equivalent,
            max_abs_diff=verdict.max_abs_diff,
            replanned=replanned,
            replan_reason=replan_reason,
            no_reuse_peak_bytes=ref_trace.peak_capacity_bytes,
        )

    def release(self, run_id: str, output: str) -> None:
        if run_id not in self._sessions:
            raise StateConflictError(f"unknown run {run_id!r}", details={"run_id": run_id})
        session = self._sessions[run_id]
        session.release_output(output)
        self.logger.event(
            run_id, "release", output=output, pinned_bytes=session.pinned_bytes()
        )

    def session(self, run_id: str) -> Session:
        if run_id not in self._sessions:
            raise StateConflictError(f"unknown run {run_id!r}", details={"run_id": run_id})
        return self._sessions[run_id]


def _plan_brief(plan: Plan) -> dict:
    return {
        "peak_bytes": plan.peak_bytes,
        "no_reuse_bytes": plan.no_reuse_bytes,
        "saved_bytes": plan.no_reuse_bytes - plan.peak_bytes,
        "buffer_count": len(plan.buffers),
    }
