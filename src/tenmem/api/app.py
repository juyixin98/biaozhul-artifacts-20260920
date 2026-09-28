"""FastAPI application: thin HTTP boundary over the Engine service.

All failures return the shared envelope
``{"success": false, "error": {"category", "message", "details"}}`` with the
status code dictated by :data:`tenmem.errors.STATUS_BY_CATEGORY`, so callers can
distinguish input_error / planning_error / replanning_required /
state_conflict / resource_exhausted / computation_failed.
"""
from __future__ import annotations

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..errors import STATUS_BY_CATEGORY, GraphValidationError, TenmemError
from ..graph import Graph
from ..runlog import RunLogger
from ..service import Engine
from .cases import CASE_NAMES, build_case
from .schemas import PlanRequest, ReleaseRequest, RunRequest, envelope

_NP = {"float32": np.float32, "float64": np.float64, "int32": np.int32, "int64": np.int64}


def create_app(log_dir: str = "logs") -> FastAPI:
    app = FastAPI(title="tenmem", version="1.0.0")
    engine = Engine(RunLogger(log_dir))
    app.state.engine = engine

    @app.exception_handler(TenmemError)
    async def handle_tenmem(_request: Request, exc: TenmemError) -> JSONResponse:
        code = STATUS_BY_CATEGORY.get(exc.category, 500)
        return JSONResponse(status_code=code, content={"success": False, "data": None, "error": exc.to_dict()})

    @app.get("/health")
    async def health() -> dict:
        return envelope({"status": "ok"})

    @app.get("/cases")
    async def cases() -> dict:
        return envelope({"cases": list(CASE_NAMES)})

    @app.post("/plan")
    async def plan(req: PlanRequest) -> dict:
        graph = Graph.from_dict(req.graph)
        plan = engine.make_plan(graph, alignment=req.alignment, max_bytes=req.max_bytes)
        return envelope(plan.to_dict())

    @app.post("/runs")
    async def create_run(req: RunRequest) -> dict:
        graph, feeds, constants = _materialize(req)
        outcome = engine.run(
            graph,
            feeds,
            constants=constants,
            parallel=req.parallel,
            alignment=req.alignment,
            max_bytes=req.max_bytes,
            run_id=req.run_id,
        )
        return envelope(
            {
                "run_id": outcome.run_id,
                "graph": outcome.graph.name,
                "outputs": {k: v.tolist() for k, v in outcome.outputs.items()},
                "reference_outputs": {k: v.tolist() for k, v in outcome.reference_outputs.items()},
                "numerically_equivalent": outcome.numerically_equivalent,
                "max_abs_diff": outcome.max_abs_diff,
                "replanned": outcome.replanned,
                "replan_reason": outcome.replan_reason,
                "plan": outcome.plan.to_dict(),
                "no_reuse_peak_bytes": outcome.no_reuse_peak_bytes,
            }
        )

    @app.post("/runs/{run_id}/release")
    async def release(run_id: str, req: ReleaseRequest) -> dict:
        engine.release(run_id, req.output)
        return envelope({"run_id": run_id, "released": req.output})

    @app.get("/runs/{run_id}/trace")
    async def trace(run_id: str) -> dict:
        records = engine.logger.replay(run_id)
        if not records:
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "data": None,
                    "error": {"category": "state_conflict", "message": f"unknown run {run_id!r}", "details": {}},
                },
            )
        return envelope({"run_id": run_id, "records": records})

    return app


def _materialize(req: RunRequest):
    constants = None
    if req.case is not None:
        if req.graph is not None:
            raise GraphValidationError("provide either case or graph, not both")
        graph, feeds = build_case(req.case, req.case_params)
    elif req.graph is not None:
        graph = Graph.from_dict(req.graph)
        feeds = _coerce_feeds(graph, req.feeds or {}, graph_inputs=True)
        if req.constants:
            constants = _coerce_feeds(graph, req.constants, graph_inputs=False)
    else:
        raise GraphValidationError("request must specify case or graph")
    return graph, feeds, constants


def _coerce_feeds(graph: Graph, raw: dict, *, graph_inputs: bool) -> dict[str, np.ndarray]:
    specs = (
        {s.name: s for s in graph.inputs}
        if graph_inputs
        else {s.name: s for s in graph.constants}
    )
    out = {}
    for name, value in raw.items():
        if name not in specs:
            kind = "input" if graph_inputs else "constant"
            raise GraphValidationError(f"unknown {kind} tensor {name!r}", details={"name": name})
        spec = specs[name]
        dtype = _NP[spec.dtype]
        arr = np.array(value, dtype=dtype)
        out[name] = arr
    return out


app = create_app()
