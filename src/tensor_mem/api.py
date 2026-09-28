"""FastAPI boundary: graph sessions, execution, handle release.

All external data is local and synthetic. The HTTP layer is a thin adapter:
it parses pydantic schemas, delegates to the planner/executor, and maps the
domain error taxonomy to distinct (status, category) responses.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

import numpy as np
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import fixtures as fx
from .errors import PlannerError
from .executor import Executor, OutputHandle
from .graph import GraphBuilder
from .runlog import RunLogger, new_run_id
from .tensor import supported_dtypes

# Category -> HTTP status. The category string is always present in the body,
# so clients distinguish failures even when statuses coincide.
_STATUS = {
    "input_error": 422,
    "graph_validation_error": 400,
    "state_conflict": 409,
    "resource_exhausted": 507,
    "bind_capacity": 500,
    "computation_failure": 422,
    "internal_error": 500,
}


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class FeedDecl(BaseModel):
    name: str
    dtype: str
    shape: list[int] = Field(min_length=1)


class NodeDecl(BaseModel):
    id: str
    op: str
    inputs: list[str]
    outputs: list[str]
    attrs: dict[str, Any] = Field(default_factory=dict)


class GraphSpec(BaseModel):
    feeds: list[FeedDecl]
    nodes: list[NodeDecl]
    outputs: list[str]
    budget: Optional[int] = Field(default=None, ge=1)
    alignment: int = 64
    external_feeds: list[str] = Field(default_factory=list)


class FeedData(BaseModel):
    data: Any  # nested JSON lists; shape/dtype come from the declaration


class ExecuteRequest(BaseModel):
    feeds: dict[str, FeedData]
    run_id: Optional[str] = None


class SyntheticRequest(BaseModel):
    case: str
    seed: int = 11
    params: dict[str, int] = Field(default_factory=dict)
    run_id: Optional[str] = None
    budget: Optional[int] = Field(default=None, ge=1)
    alignment: int = 64


class ReleaseRequest(BaseModel):
    run_id: str


# --------------------------------------------------------------------------- #
# Session registry
# --------------------------------------------------------------------------- #


class _Session:
    def __init__(self, spec: GraphSpec, executor: Executor, run_log: RunLogger) -> None:
        self.spec = spec
        self.executor = executor
        self.run_log = run_log
        self.handles: dict[str, dict[str, OutputHandle]] = {}


class ApiState:
    def __init__(self, log_dir: str = "logs") -> None:
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        self.log_dir = log_dir

    def create(self, spec: GraphSpec) -> tuple[str, _Session]:
        builder = GraphBuilder()
        for f in spec.feeds:
            builder.feed(f.name, f.dtype, f.shape)
        for n in spec.nodes:
            builder.node(n.id, n.op, n.inputs, n.outputs, n.attrs)
        builder.graph_outputs(spec.outputs)
        graph = builder.build()

        session_id = new_run_id("sess")
        run_log = RunLogger(
            run_id=session_id, path=f"{self.log_dir}/{session_id}.jsonl"
        )
        executor = Executor(
            graph,
            budget=spec.budget,
            alignment=spec.alignment,
            external_names=frozenset(spec.external_feeds),
            logger=run_log,
        )
        run_log.event(
            "session_created", "graph planned from spec",
            session_id=session_id,
            nodes=list(graph.order),
            waves=[list(w) for w in graph.waves],
            plan=executor.plan.summary(),
        )
        session = _Session(spec, executor, run_log)
        with self._lock:
            self._sessions[session_id] = session
        return session_id, session

    def create_from_case(
        self,
        case_name: str,
        seed: int,
        params: dict[str, int],
        budget: Optional[int],
        alignment: int,
    ) -> tuple[str, _Session, fx.Case]:
        """Build a session directly from a built-in synthetic fixture."""
        case = _build_synthetic_case(case_name, seed, params)
        graph = case.graph()
        session_id = new_run_id("sess")
        run_log = RunLogger(
            run_id=session_id, path=f"{self.log_dir}/{session_id}.jsonl"
        )
        executor = Executor(
            graph, budget=budget, alignment=alignment, logger=run_log
        )
        run_log.event(
            "session_created", "graph planned from synthetic fixture",
            session_id=session_id, case=case_name, seed=seed, params=params,
            nodes=list(graph.order),
            waves=[list(w) for w in graph.waves],
            plan=executor.plan.summary(),
        )
        session = _Session(GraphSpec(
            feeds=[], nodes=[], outputs=list(graph.graph_outputs),
            budget=budget, alignment=alignment,
        ), executor, run_log)
        with self._lock:
            self._sessions[session_id] = session
        return session_id, session, case

    def get(self, session_id: str) -> _Session:
        with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError:
                from .errors import InputValidationError

                raise InputValidationError(
                    "unknown session", session_id=session_id
                ) from None


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


def create_app(log_dir: str = "logs") -> FastAPI:
    app = FastAPI(title="tensor-mem-reuse", version="0.1.0")
    state = ApiState(log_dir=log_dir)
    app.state.api = state

    @app.exception_handler(PlannerError)
    async def planner_error_handler(request, exc: PlannerError):
        status = _STATUS.get(exc.category, 500)
        return JSONResponse(status_code=status, content=exc.to_dict())

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "dtypes": supported_dtypes(),
            "sessions": len(state._sessions),
        }

    @app.get("/ops")
    async def list_ops():
        from .ops import REGISTRY

        return {
            name: {
                "inputs": spec.ninputs,
                "outputs": spec.noutputs,
                "alias": spec.alias,
                "shape_sensitive": spec.shape_sensitive,
            }
            for name, spec in sorted(REGISTRY.items())
        }

    @app.post("/sessions", status_code=201)
    async def create_session(spec: GraphSpec):
        session_id, session = state.create(spec)
        return {
            "session_id": session_id,
            "plan": session.executor.plan.summary(),
            "waves": [list(w) for w in session.executor.graph.waves],
            "log": str(session.run_log.path),
        }

    @app.post("/sessions/{session_id}/execute")
    async def execute(session_id: str, req: ExecuteRequest):
        session = state.get(session_id)
        arrays: dict[str, np.ndarray] = {}
        for name, payload in req.feeds.items():
            if name not in session.executor.graph.feeds:
                from .errors import InputValidationError

                raise InputValidationError(
                    "unknown feed", feed=name,
                    known=sorted(session.executor.graph.feeds),
                )
            declared = session.executor.graph.feeds[name]
            # Static feed shapes act only as rank/dtype contract; concrete
            # dimensions may differ and trigger replanning downstream.
            arr = np.asarray(payload.data, dtype=declared.tensor_type.numpy_dtype)
            arrays[name] = arr

        handles, report = session.executor.execute(arrays, run_id=req.run_id)
        session.handles[report.run_id] = handles
        return _execution_payload(handles, report)

    @app.post("/synthetic", status_code=201)
    async def run_synthetic(req: SyntheticRequest):
        """Create a session for a built-in fixture and execute it in one call,
        using only local synthetic data."""
        session_id, session, case = state.create_from_case(
            req.case, req.seed, req.params, req.budget, req.alignment
        )
        handles, report = session.executor.execute(case.feeds, run_id=req.run_id)
        session.handles[report.run_id] = handles
        payload = _execution_payload(handles, report)
        payload["session_id"] = session_id
        payload["case"] = req.case
        return payload

    @app.post("/sessions/{session_id}/release")
    async def release(session_id: str, req: ReleaseRequest):
        session = state.get(session_id)
        if req.run_id not in session.handles:
            from .errors import StateConflictError

            raise StateConflictError(
                "no retained outputs for run in this session",
                run_id=req.run_id,
            )
        handles = session.handles.pop(req.run_id)
        for h in handles.values():
            h.release()
        return {
            "released_run": req.run_id,
            "retained_bytes": session.executor.retained_bytes(),
        }

    @app.get("/sessions/{session_id}")
    async def inspect(session_id: str):
        session = state.get(session_id)
        return {
            "session_id": session_id,
            "plan": session.executor.plan.summary(),
            "retained_bytes": session.executor.retained_bytes(),
            "runs": list(session.handles),
        }

    return app


def _build_synthetic_case(case_name: str, seed: int, params: dict[str, int]) -> fx.Case:
    if case_name == "diamond":
        return fx.diamond_case(seed)
    if case_name == "long_lived":
        return fx.long_lived_case(seed)
    if case_name == "dynamic_matmul":
        return fx.dynamic_case(
            m=int(params.get("m", 8)),
            k=int(params.get("k", 4)),
            n=int(params.get("n", 2)),
            seed=seed,
        )
    if case_name == "concurrent_branches":
        return fx.concurrent_case(
            branches=int(params.get("branches", 3)), seed=seed
        )
    if case_name == "workspace":
        return fx.workspace_case(seed)
    if case_name == "alias":
        return fx.alias_case(seed)
    if case_name == "linear_grad":
        return fx.linear_grad_case(
            r=int(params.get("r", 3)),
            c=int(params.get("c", 4)),
            b=int(params.get("b", 2)),
            seed=seed,
        )
    from .errors import InputValidationError

    raise InputValidationError(
        "unknown synthetic case",
        case=case_name,
        known=["diamond", "long_lived", "dynamic_matmul",
               "concurrent_branches", "workspace", "alias", "linear_grad"],
    )


def _execution_payload(
    handles: dict[str, OutputHandle], report
) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "replanned": report.replanned,
        "capacity_deficits": report.capacity_deficits,
        "plan_peak_resident_bytes": report.plan_peak_resident,
        "retained_before_bytes": report.retained_before_bytes,
        "charged_peak_bytes": report.charged_peak_bytes,
        "budget": report.budget,
        "output_shapes": {k: list(v) for k, v in report.output_shapes.items()},
        "outputs": {name: _array_to_json(h.array) for name, h in handles.items()},
        "waves": report.wave_resident,
        "nodes": [
            {
                "node": t.node_id, "op": t.op, "wave": t.wave,
                "input_shapes": t.input_shapes,
                "output_shapes": t.output_shapes,
                "slots": t.slots,
            }
            for t in report.node_traces
        ],
    }


def _array_to_json(arr: np.ndarray) -> dict[str, Any]:
    return {
        "dtype": str(arr.dtype),
        "shape": list(arr.shape),
        "data": arr.tolist(),
    }


app = create_app()
