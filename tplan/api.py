"""FastAPI query interface.

Endpoints
---------
``GET  /health``                 service + engine versions
``POST /api/v1/problems/solve``  parse, solve, independently replay, store
``POST /api/v1/problems/validate``  parse only
``GET  /api/v1/runs``            recent run summaries
``GET  /api/v1/runs/{run_id}``   full evidence: problem, schedule, timeline, trace

Every solve response includes ``run_id`` and ``input_sha256``; failure
responses carry an explicit ``failure_code`` instead of a generic error.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import __version__
from .config import SETTINGS, Settings
from .schemas import HealthResponse, ReplayRequest, RunSummary, SolveRequest
from .service import PlanningService
from .store import EvidenceStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("tplan.api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Path(SETTINGS.log_dir).mkdir(parents=True, exist_ok=True)
    Path(SETTINGS.db_path).parent.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="Temporal Planning Service (finite action set)",
    version=__version__,
    description=(
        "Start conditions, durational invariants and end effects over a small "
        "integer time grid, with half-open resource intervals, an explicit "
        "simultaneous-event policy, budget-aware optimality reporting and an "
        "independently replayed evidence timeline."
    ),
    lifespan=lifespan,
)


def get_settings() -> Settings:
    return SETTINGS


def get_service(settings: Settings = Depends(get_settings)) -> PlanningService:
    # One connection per request; SQLite handles this trivially at local scale.
    store = EvidenceStore(settings.db_path)
    try:
        yield PlanningService(settings, store)
    finally:
        store.close()


@app.get("/health", response_model=HealthResponse)
def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    import platform

    return HealthResponse(
        status="ok", service=settings.service_name, version=__version__,
        python=platform.python_version(),
    )


@app.post("/api/v1/problems/validate")
def validate(body: SolveRequest, service: PlanningService = Depends(get_service)) -> dict:
    try:
        problem = service.validate_only(body.problem)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail={"failure_code": "invalid_problem", "message": str(exc)})
    return {
        "valid": True,
        "horizon": problem.horizon,
        "actions": list(problem.action_order),
        "fluents": sorted(problem.fluents),
        "resources": sorted(problem.resources),
    }


@app.post("/api/v1/problems/solve")
def solve(body: SolveRequest, service: PlanningService = Depends(get_service)) -> dict:
    try:
        resp = service.solve(
            body.problem,
            node_budget=body.node_budget,
            time_budget_seconds=body.time_budget_seconds,
        )
    except Exception as exc:  # never collapse an unknown fault into success
        log.exception("unexpected internal error")
        raise HTTPException(
            status_code=500,
            detail={"failure_code": "internal_error", "message": f"{type(exc).__name__}: {exc}"},
        )
    status_code = 200 if resp.status != "invalid" else 422
    return JSONResponse(status_code=status_code, content=resp.__dict__)


@app.post("/api/v1/problems/replay")
def replay(body: ReplayRequest, service: PlanningService = Depends(get_service)) -> dict:
    try:
        result = service.replay(body.problem, [s.__dict__ for s in body.schedule])
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"failure_code": "invalid_problem", "message": str(exc)},
        )
    return JSONResponse(status_code=200 if result["ok"] else 409, content=result)


@app.get("/api/v1/runs", response_model=list[RunSummary])
def list_runs(limit: int = 50, service: PlanningService = Depends(get_service)) -> list[dict]:
    limit = max(1, min(limit, 200))
    return service.store.list_runs(limit)


@app.get("/api/v1/runs/{run_id}")
def get_run(run_id: str, service: PlanningService = Depends(get_service)) -> dict:
    record = service.store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"failure_code": "not_found", "message": run_id})
    return record
