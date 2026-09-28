"""FastAPI query interface over the reasoning kernel and evidence store."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .evidence import EvidenceStore
from .kernel.solver import SolveConfig, Solver
from .model import Problem
from .runlog import tool_versions


class SolveRequest(BaseModel):
    problem: Problem | None = None
    problem_name: str | None = None
    mode: Literal["first", "all"] = "first"
    max_nodes: int | None = Field(default=None, ge=1)
    max_propagation_steps: int | None = Field(default=None, ge=1)
    max_solutions: int | None = Field(default=None, ge=1)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = EvidenceStore(settings.db_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        store.close()

    app = FastAPI(title="finite-domain-csp-service", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.problems: dict[str, dict] = {}

    def get_store() -> EvidenceStore:
        return app.state.store

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "versions": tool_versions()}

    @app.post("/problems", status_code=201)
    def register_problem(problem: Problem) -> dict:
        app.state.problems[problem.name] = problem.model_dump()
        return {"registered": problem.name}

    @app.get("/problems/{name}")
    def get_problem(name: str) -> dict:
        if name not in app.state.problems:
            raise HTTPException(404, f"unknown problem: {name}")
        return app.state.problems[name]

    @app.post("/solve")
    def solve(req: SolveRequest, store: EvidenceStore = Depends(get_store)) -> dict:
        if req.problem is None and req.problem_name is None:
            raise HTTPException(400, "provide either problem or problem_name")
        if req.problem is not None:
            problem = req.problem
        else:
            raw = app.state.problems.get(req.problem_name)
            if raw is None:
                raise HTTPException(404, f"unknown problem: {req.problem_name}")
            try:
                problem = Problem(**raw)
            except ValidationError as exc:  # pragma: no cover - defensive
                raise HTTPException(422, exc.errors())
        config = SolveConfig(
            mode=req.mode,
            max_nodes=req.max_nodes or settings.default_max_nodes,
            max_propagation_steps=(
                req.max_propagation_steps or settings.default_max_propagation_steps
            ),
            max_solutions=req.max_solutions,
        )
        result = Solver(problem, config).solve()
        run_id = store.new_run_id()
        store.save_run(
            run_id=run_id,
            problem_name=problem.name,
            problem=problem.model_dump(),
            config={
                "mode": config.mode,
                "max_nodes": config.max_nodes,
                "max_propagation_steps": config.max_propagation_steps,
                "max_solutions": config.max_solutions,
            },
            versions=tool_versions(),
            status=result.status,
            partial=result.partial,
            stats=result.stats,
            events=result.events,
            solutions=result.solutions,
        )
        return {
            "run_id": run_id,
            "status": result.status,
            "partial": result.partial,
            "solutions": result.solutions,
            "stats": result.stats,
        }

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, store: EvidenceStore = Depends(get_store)) -> dict:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"unknown run: {run_id}")
        run["solutions"] = store.get_solutions(run_id)
        return run

    @app.get("/runs/{run_id}/events")
    def get_events(
        run_id: str, kind: str | None = None, store: EvidenceStore = Depends(get_store)
    ) -> dict:
        if store.get_run(run_id) is None:
            raise HTTPException(404, f"unknown run: {run_id}")
        return {"run_id": run_id, "events": store.get_events(run_id, kind)}

    @app.get("/runs/{run_id}/prunings")
    def get_prunings(run_id: str, store: EvidenceStore = Depends(get_store)) -> dict:
        if store.get_run(run_id) is None:
            raise HTTPException(404, f"unknown run: {run_id}")
        prunings = [
            e for e in store.get_events(run_id)
            if e["kind"] in ("prune", "wipeout", "hall_violation")
        ]
        return {"run_id": run_id, "prunings": prunings}

    return app


app = create_app()
