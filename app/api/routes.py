"""HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import (
    FixtureListResponse,
    RunDetail,
    SolveRequest,
    SolveResponse,
)
from app.fixtures import CATALOG, get_fixture

router = APIRouter()


@router.post("/solve", response_model=SolveResponse)
def solve(request: SolveRequest, http_request: Request) -> dict:
    settings = http_request.app.state.settings
    model = request.model
    if model.variable_count() > settings.max_variables:
        raise HTTPException(
            status_code=422,
            detail=f"too many variables: {model.variable_count()} "
            f"> {settings.max_variables}",
        )
    oversized = {
        variable: len(domain)
        for variable, domain in model.domains.items()
        if len(domain) > settings.max_domain_size
    }
    if oversized:
        raise HTTPException(
            status_code=422,
            detail=f"domains exceed {settings.max_domain_size} values: {oversized}",
        )
    service = http_request.app.state.solve_service
    _run_id, response = service.solve(
        model=model,
        max_nodes=request.max_nodes,
        max_backtracks=request.max_backtracks,
        collect_reasons=request.collect_reasons,
    )
    return response


@router.get("/runs", response_model=list)
def list_runs(http_request: Request, limit: int = 50) -> list[dict]:
    store = http_request.app.state.evidence_store
    return store.list_runs(limit=limit)


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, http_request: Request) -> dict:
    store = http_request.app.state.evidence_store
    record = store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown run_id {run_id}")
    return {
        "run_id": record.run_id,
        "created_at": record.created_at,
        "model_name": record.model_name,
        "model": record.model,
        "status": record.status,
        "solution": record.solution,
        "stats": record.stats,
        "failure": record.failure,
        "solver_version": record.solver_version,
        "reasons": store.get_reasons(run_id),
    }


@router.get("/fixtures", response_model=FixtureListResponse)
def list_fixtures() -> dict:
    return {"fixtures": sorted(CATALOG)}


@router.get("/fixtures/{name}")
def get_fixture_payload(name: str) -> dict:
    try:
        return get_fixture(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown fixture {name}")
