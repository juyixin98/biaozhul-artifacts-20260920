"""FastAPI query interface for the finite HTN planner.

Endpoints
---------
GET  /health                      service identity + loaded domains
GET  /domains                     available fixture domains
POST /plan/fixture                plan a bundled problem fixture
POST /plan/inline                 plan an ad-hoc problem body
GET  /plans                       recent runs (evidence index)
GET  /plans/{rid}                 full stored result
GET  /plans/{rid}/tree            retained abstract->primitive expansion tree
GET  /plans/{rid}/failures        terminal failures + abandoned branches
GET  /plans/{rid}/audit           ordered key-step audit log

Every response carries the request identity and service version.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field, ValidationError

from .config import SETTINGS
from .models import Problem
from .service import PlanningService, ServiceError
from .storage import EvidenceStore


class FixturePlanRequest(BaseModel):
    problem: str = Field(..., description="Fixture problem name or file name")
    request_id: str | None = None


class InlinePlanRequest(BaseModel):
    problem: dict[str, Any]
    request_id: str | None = None


def create_app(
    service: PlanningService | None = None,
    store: EvidenceStore | None = None,
) -> FastAPI:
    settings = SETTINGS
    svc = service or PlanningService(settings=settings, store=store)

    app = FastAPI(
        title="Finite HTN Planner",
        version=settings.service_version,
        description="Recursive finite HTN with method selection and partial order",
    )

    @app.middleware("http")
    async def add_identity_headers(request, call_next):  # noqa: ANN001
        response: Response = await call_next(request)
        response.headers["x-service-name"] = settings.service_name
        response.headers["x-service-version"] = settings.service_version
        return response

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "service": settings.service_name,
            "version": settings.service_version,
            "domains": svc.registry.names(),
        }

    @app.get("/domains")
    def domains() -> dict:
        return {"domains": svc.registry.names()}

    @app.post("/plan/fixture")
    def plan_fixture(req: FixturePlanRequest) -> dict:
        try:
            result = svc.plan_from_fixture(req.problem, req.request_id)
        except ServiceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return result.model_dump()

    @app.post("/plan/inline")
    def plan_inline(req: InlinePlanRequest) -> dict:
        try:
            problem = Problem.model_validate(req.problem)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        try:
            result = svc.plan_inline(problem, req.request_id)
        except ServiceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return result.model_dump()

    @app.get("/plans")
    def list_plans(limit: int = 50) -> dict:
        return {"plans": svc.store.list_plans(limit=min(limit, 200))}

    @app.get("/plans/{request_id}")
    def get_plan(request_id: str) -> dict:
        payload = svc.store.get_payload(request_id)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"no run {request_id}")
        return payload

    @app.get("/plans/{request_id}/tree")
    def get_tree(request_id: str) -> dict:
        tree = svc.store.get_tree(request_id)
        if tree is None:
            raise HTTPException(status_code=404, detail=f"no run {request_id}")
        return tree

    @app.get("/plans/{request_id}/failures")
    def get_failures(request_id: str) -> dict:
        if svc.store.get_summary(request_id) is None:
            raise HTTPException(status_code=404, detail=f"no run {request_id}")
        return {"request_id": request_id, "evidence": svc.store.get_failures(request_id)}

    @app.get("/plans/{request_id}/audit")
    def get_audit(request_id: str) -> dict:
        if svc.store.get_summary(request_id) is None:
            raise HTTPException(status_code=404, detail=f"no run {request_id}")
        return {"request_id": request_id, "audit": svc.store.get_audit(request_id)}

    return app


app = create_app()
