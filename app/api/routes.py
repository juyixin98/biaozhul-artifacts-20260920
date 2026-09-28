"""HTTP routes.

The API is deliberately thin: validation by Pydantic (contracts), work by the
service layer, persistence by the ledger. Every response or rejection carries
the request identity and core version, and every run is written to the SQLite
ledger and the JSON log.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.contracts.models import (
    DIDRequest,
    EventStudyRequest,
    FailureCategory,
    FailureEnvelope,
)
from app.core.errors import EstimationError
from app.reproducibility import build_fixture, fixture_provenance, list_fixtures
from app.service import run_did, run_event_study

router = APIRouter(prefix="/api/v1")

# Categories that describe malformed input (400) vs a statistically valid
# request the model refuses to answer (422). The distinction is reported, never
# collapsed into a generic 500.
_BAD_REQUEST = {
    FailureCategory.EMPTY_PANEL,
    FailureCategory.DUPLICATE_UNIT_PERIOD,
    FailureCategory.NON_FINITE_OUTCOME,
    FailureCategory.INVALID_WEIGHT,
    FailureCategory.NOT_TWO_PERIODS,
    FailureCategory.SINGLE_PERIOD,
    FailureCategory.INVALID_REQUEST,
}


def _http_status(category: FailureCategory) -> int:
    return 400 if category in _BAD_REQUEST else 422


def _envelope(err: EstimationError, request_id: str, core_version: str) -> FailureEnvelope:
    return FailureEnvelope(
        request_id=request_id,
        core_version=core_version,
        failure_category=err.category,
        message=err.message,
        diagnostics=err.diagnostics,
        excluded=err.excluded,
        steps=err.steps,
    )


def _ledger() -> Any:
    from app.main import get_ledger

    return get_ledger()


def _logger(request: Request) -> logging.Logger:
    from app.main import get_logger

    return get_logger()


def _record(request: Request, endpoint: str, payload: dict[str, Any]) -> None:
    try:
        _ledger().record_run({"endpoint": endpoint, **payload})
    except Exception as exc:  # noqa: BLE001 - ledger must never break the response
        _logger(request).error("ledger_write_failed", extra={"detail": str(exc)})


# --------------------------------------------------------------------------- #
# Health / meta
# --------------------------------------------------------------------------- #
@router.get("/health")
def health() -> dict[str, str]:
    from app.config import CONFIG

    return {"status": "ok", "service": CONFIG.service_name, "core_version": CONFIG.core_version}


@router.get("/fixtures")
def fixtures() -> dict[str, Any]:
    return {"fixtures": list_fixtures()}


@router.get("/fixtures/{name}")
def fixture_detail(name: str) -> dict[str, Any]:
    if name not in list_fixtures():
        raise HTTPException(status_code=404, detail=f"unknown fixture {name!r}")
    obs = build_fixture(name)
    prov = fixture_provenance(name)
    return {
        "name": name,
        "provenance": prov.as_dict(),
        "observations": [o.model_dump() for o in obs],
    }


# --------------------------------------------------------------------------- #
# DID
# --------------------------------------------------------------------------- #
@router.post("/did", response_model=None)
def did_endpoint(body: DIDRequest, request: Request) -> JSONResponse:
    log = _logger(request)
    log.info(
        "did_request_received",
        extra={"request_id": body.request_id, "endpoint": "/api/v1/did", "n_obs": len(body.observations)},
    )
    try:
        result = run_did(body)
    except EstimationError as err:
        env = _envelope(err, body.request_id, _core_version())
        _record(
            request,
            "/api/v1/did",
            {
                "request_id": body.request_id,
                "status": "rejected",
                "failure_category": err.category.value,
                "core_version": env.core_version,
                "steps": [s.model_dump() for s in err.steps],
                "excluded": [e.model_dump() for e in err.excluded],
                "diagnostics": [d.model_dump() for d in err.diagnostics],
            },
        )
        log.warning(
            "did_request_rejected",
            extra={
                "request_id": body.request_id,
                "status": "rejected",
                "failure_category": err.category.value,
            },
        )
        return JSONResponse(status_code=_http_status(err.category), content=env.model_dump())

    _record(
        request,
        "/api/v1/did",
        {
            "request_id": result.request_id,
            "status": result.status,
            "failure_category": None,
            "core_version": result.core_version,
            "point_estimate": result.estimate.value if result.estimate else None,
            "standard_error": result.estimate.se if result.estimate else None,
            "n_units": result.estimate.n_units if result.estimate else None,
            "n_clusters": result.estimate.n_clusters if result.estimate else None,
            "steps": [s.model_dump() for s in result.steps],
            "excluded": [e.model_dump() for e in result.excluded],
            "diagnostics": [d.model_dump() for d in result.diagnostics],
        },
    )
    log.info(
        "did_request_ok",
        extra={
            "request_id": result.request_id,
            "status": "ok",
            "detail": f"DID={result.estimate.value if result.estimate else None}",
        },
    )
    return JSONResponse(status_code=200, content=result.model_dump())


# --------------------------------------------------------------------------- #
# Event study
# --------------------------------------------------------------------------- #
@router.post("/event-study", response_model=None)
def event_study_endpoint(body: EventStudyRequest, request: Request) -> JSONResponse:
    log = _logger(request)
    log.info(
        "event_study_request_received",
        extra={"request_id": body.request_id, "endpoint": "/api/v1/event-study"},
    )
    try:
        result = run_event_study(body)
    except EstimationError as err:
        env = _envelope(err, body.request_id, _core_version())
        _record(
            request,
            "/api/v1/event-study",
            {
                "request_id": body.request_id,
                "status": "rejected",
                "failure_category": err.category.value,
                "core_version": env.core_version,
                "steps": [s.model_dump() for s in err.steps],
                "excluded": [e.model_dump() for e in err.excluded],
                "diagnostics": [d.model_dump() for d in err.diagnostics],
            },
        )
        log.warning(
            "event_study_rejected",
            extra={"request_id": body.request_id, "failure_category": err.category.value},
        )
        return JSONResponse(status_code=_http_status(err.category), content=env.model_dump())

    _record(
        request,
        "/api/v1/event-study",
        {
            "request_id": result.request_id,
            "status": result.status,
            "core_version": result.core_version,
            "steps": [s.model_dump() for s in result.steps],
            "excluded": [e.model_dump() for e in result.excluded],
            "diagnostics": [d.model_dump() for d in result.diagnostics],
        },
    )
    log.info("event_study_ok", extra={"request_id": result.request_id, "status": "ok"})
    return JSONResponse(status_code=200, content=result.model_dump())


# --------------------------------------------------------------------------- #
# Ledger lookup
# --------------------------------------------------------------------------- #
@router.get("/runs/{request_id}")
def get_run(request_id: str) -> dict[str, Any]:
    row = _ledger().fetch_run(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no run for request_id={request_id!r}")
    return row


def _core_version() -> str:
    from app.config import CONFIG

    return CONFIG.core_version
