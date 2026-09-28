"""HTTP routes. Thin layer: delegate to ReasoningService."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from ..services import ReasoningService
from .deps import get_service
from .schemas import OntologyRequest, SubclassRequest

router = APIRouter()
logger = logging.getLogger("owl.api")


def _request_id(x_request_id: str | None) -> str:
    if x_request_id:
        # keep caller-supplied correlation id but namespace it predictably
        return x_request_id.strip()[:64]
    return ""  # service will generate


@router.post("/reason", summary="Classify an ontology")
async def reason_endpoint(
    body: OntologyRequest,
    x_request_id: str | None = Header(default=None),
    service: ReasoningService = Depends(get_service),
) -> dict[str, Any]:
    rid = _request_id(x_request_id) or None
    payload = body.model_dump()
    response = service.classify(payload, request_id=rid)
    # rejected inputs are a 422 with the structured, path-precise error
    if response["status"] == "rejected":
        return JSONResponse(status_code=422, content=response)
    return response


@router.post("/subclass", summary="Ask whether sub ⊑ super is entailed")
async def subclass_endpoint(
    body: SubclassRequest,
    x_request_id: str | None = Header(default=None),
    service: ReasoningService = Depends(get_service),
) -> dict[str, Any]:
    rid = _request_id(x_request_id) or None
    data = body.model_dump()
    sub, sup = data.pop("sub"), data.pop("super")
    response = service.subsumption_query(
        data, sub=sub, sup=sup, request_id=rid
    )
    if response["status"] == "rejected":
        return JSONResponse(status_code=422, content=response)
    return response


@router.get("/requests/{request_id}", summary="Retrieve an audited request")
async def get_request(
    request_id: str,
    service: ReasoningService = Depends(get_service),
) -> JSONResponse:
    record = service.store.get_request(request_id)
    if record is None:
        return JSONResponse(
            status_code=404,
            content={
                "request_id": request_id,
                "status": "not_found",
                "failure_categories": ["REQUEST_NOT_FOUND"],
                "uncertainties": [],
            },
        )
    return JSONResponse(status_code=200, content=record)


@router.get("/health", summary="Liveness/version probe")
async def health(service: ReasoningService = Depends(get_service)) -> dict[str, str]:
    from .. import ENGINE_VERSION
    return {
        "status": "alive",
        "engine_version": ENGINE_VERSION,
        "stored_requests": str(service.store.count_requests()),
    }
