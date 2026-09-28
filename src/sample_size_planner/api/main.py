"""FastAPI application: HTTP transport over the planning service.

Endpoints:
    POST /plan/normal      sample size for a composite normal test
    POST /plan/binomial    sample size for a proportion test
    POST /simulate/power   independent Monte Carlo evidence
    POST /interim/plan     committed group-sequential boundary
    GET  /runs/{run_id}    persisted audit record
    GET  /health           liveness + dependency versions

Failures are returned with their explicit failure category and HTTP semantics;
an unknown/exceptional state is a 500 error envelope, never a 200 success.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ..contracts import FailureCategory
from ..evidence.run_log import dependency_versions
from .schemas import (
    BinomialPlanRequest,
    ErrorEnvelope,
    InterimPlanRequest,
    InterimResponse,
    NormalPlanRequest,
    PlanResponse,
    SimulationRequest,
    SimulationResponse,
)
from .service import PlanningService


def create_app(service: PlanningService | None = None) -> FastAPI:
    app = FastAPI(
        title="Sample Size Planner",
        version="1.0.0",
        description="Sample-size planning for composite normal and binomial tests.",
    )
    app.state.service = service or PlanningService()

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"status": "ok", "versions": dependency_versions()}

    @app.post("/plan/normal", response_model=PlanResponse)
    def plan_normal(req: NormalPlanRequest) -> Dict[str, Any]:
        return app.state.service.plan_normal(req)

    @app.post("/plan/binomial", response_model=PlanResponse)
    def plan_binomial(req: BinomialPlanRequest) -> Dict[str, Any]:
        return app.state.service.plan_binomial(req)

    @app.post("/simulate/power", response_model=SimulationResponse)
    def simulate_power(req: SimulationRequest) -> Dict[str, Any]:
        try:
            return app.state.service.run_simulation(req)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={
                "failure_category": FailureCategory.INVALID_INPUT.value,
                "error": str(exc),
            }) from exc

    @app.post("/interim/plan", response_model=InterimResponse)
    def interim_plan(req: InterimPlanRequest) -> Dict[str, Any]:
        out = app.state.service.plan_interim(req)
        if not out.get("success", True):
            return JSONResponse(status_code=422, content=ErrorEnvelope(
                run_id=out.get("run_id"), error="interim commitment violation",
                failure_category=out.get("failure_category", FailureCategory.COMPUTATION_ERROR.value),
                detail=out.get("detail", "")).model_dump())
        return out

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> Dict[str, Any]:
        record = app.state.service.repo.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail={
                "failure_category": FailureCategory.NONE.value,
                "error": f"unknown run_id {run_id}",
            })
        record["plans"] = app.state.service.repo.list_plans(run_id)
        return record

    return app


app = create_app()


def run() -> None:  # console-script entry point
    import uvicorn

    from ..config import settings

    uvicorn.run("sample_size_planner.api.main:app", host=settings.host,
                port=settings.port, reload=False)
