"""HTTP routes for problem management, propagation and ATMS queries."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from ..core.budgets import Budgets
from ..config import settings
from ..rules.language import RuleLanguageError
from ..storage.repository import ProblemNotFound
from ..services.engine_service import label_payload, nogoods_json
from .deps import request_id_dep
from .models import CreateProblemRequest, PropagateRequest, QueryRequest, RetractRequest

logger = logging.getLogger("atms")

router = APIRouter()


def _service(request: Request):
    return request.app.state.service


def _not_found(problem_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"problem not found: {problem_id}")


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/problems", status_code=201)
def create_problem(
    body: CreateProblemRequest,
    request: Request,
    request_id: str = Depends(request_id_dep),
) -> dict:
    svc = _service(request)
    if svc.repo.problem_exists(body.id):
        raise HTTPException(status_code=409, detail=f"problem exists: {body.id}")
    try:
        result = svc.create_problem(body.id, body.name, body.source)
    except RuleLanguageError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "request_id": request_id,
                "error": "rule_language_error",
                "line": exc.line_no,
                "message": str(exc),
            },
        )
    return {"request_id": request_id, **result}


@router.get("/problems")
def list_problems(request: Request) -> dict:
    return {"problems": _service(request).repo.list_problems()}


@router.post("/problems/{problem_id}/propagate")
def propagate(
    problem_id: str,
    request: Request,
    body: Optional[PropagateRequest] = None,
    request_id: str = Depends(request_id_dep),
) -> dict:
    svc = _service(request)
    try:
        override = None
        if body is not None and body.model_dump(exclude_none=True):
            b = svc.budgets
            override = Budgets(
                max_label_envs=body.max_label_envs or b.max_label_envs,
                max_total_envs=body.max_total_envs or b.max_total_envs,
                max_steps=body.max_steps or b.max_steps,
            )
        _, summary = svc.propagate(
            problem_id, request_id, budgets_override=override
        )
        return summary
    except ProblemNotFound:
        raise _not_found(problem_id)


@router.get("/problems/{problem_id}/labels")
def labels(problem_id: str, request: Request) -> dict:
    svc = _service(request)
    try:
        engine = svc.current_engine(problem_id)
    except ProblemNotFound:
        raise _not_found(problem_id)
    return {
        "problem_id": problem_id,
        "labels": label_payload(engine),
        "nogoods": nogoods_json(engine),
        "incomplete": engine.incomplete,
    }


@router.post("/problems/{problem_id}/query")
def query_node(
    problem_id: str,
    body: QueryRequest,
    request: Request,
    request_id: str = Depends(request_id_dep),
) -> dict:
    svc = _service(request)
    try:
        engine, record = svc.query_node(
            problem_id,
            body.node_id,
            context=body.environment,
            request_id=request_id,
        )
    except ProblemNotFound:
        raise _not_found(problem_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if settings.log_redact:
        logger.info("decision %s", record.as_log_dict())
    else:
        logger.info("decision %s", asdict(record))
    return asdict(record)


@router.get("/problems/{problem_id}/nodes/{node_id}/explain")
def explain(problem_id: str, node_id: str, request: Request) -> dict:
    svc = _service(request)
    try:
        return svc.explain_node(problem_id, node_id)
    except ProblemNotFound:
        raise _not_found(problem_id)


@router.get("/problems/{problem_id}/nogoods")
def nogoods(problem_id: str, request: Request) -> dict:
    svc = _service(request)
    try:
        return svc.nogoods(problem_id)
    except ProblemNotFound:
        raise _not_found(problem_id)


@router.post("/problems/{problem_id}/retract")
def retract(
    problem_id: str,
    body: RetractRequest,
    request: Request,
    request_id: str = Depends(request_id_dep),
) -> dict:
    svc = _service(request)
    try:
        return svc.retract_assumptions(
            problem_id, body.assumptions, request_id=request_id
        )
    except ProblemNotFound:
        raise _not_found(problem_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc).strip("'"))


@router.get("/problems/{problem_id}/runs")
def runs(problem_id: str, request: Request, limit: int = 20) -> dict:
    svc = _service(request)
    try:
        return {"runs": svc.repo.list_runs(problem_id, limit=limit)}
    except ProblemNotFound:
        raise _not_found(problem_id)
