"""FastAPI application wiring the Datalog service to HTTP.

Endpoints
---------
``POST /query``       compile + evaluate a program and answer one goal
``POST /compile``     compile only (safety / stratification diagnostics)
``POST /materialize`` evaluate and persist the whole closure
``GET  /requests/{id}``            stored request row
``GET  /requests/{id}/summary``    relation counts for a request
``GET  /requests/{id}/derivation`` one persisted tuple + its SQL support rows
``GET  /health``      liveness

All request bodies accept an optional ``request_id``; when omitted one is
generated and returned.  Failures return HTTP 200 with a typed
``failures`` list for compile/query semantics (the request was understood,
the program is invalid) and HTTP 4xx only for malformed payloads.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..service import DatalogService
from ..store.sqlite_store import EvidenceStore
from ..config import Settings
from ..logging_setup import setup_logging

settings = Settings.from_env()
setup_logging(settings.log_level, settings.log_file)

store = EvidenceStore(settings.db_path)
service = DatalogService(store, program_id=settings.program_id)

app = FastAPI(title="Restricted Datalog Query Service", version="1.0.0")


class QueryRequest(BaseModel):
    program: str = Field(..., description="Datalog source: facts and rules")
    goal: str = Field(..., description="query atom, e.g. ancestor(ann, X)")
    request_id: str | None = None
    persist: bool = True


class ProgramRequest(BaseModel):
    program: str
    request_id: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.post("/query")
def query(req: QueryRequest) -> dict[str, Any]:
    resp = service.run_query(req.program, req.goal, request_id=req.request_id, persist=req.persist)
    return resp.to_dict()


@app.post("/compile")
def compile_only(req: ProgramRequest) -> dict[str, Any]:
    return service.compile_only(req.program, request_id=req.request_id).to_dict()


@app.post("/materialize")
def materialize(req: ProgramRequest) -> dict[str, Any]:
    return service.materialize(req.program, request_id=req.request_id).to_dict()


@app.get("/requests/{request_id}")
def get_request(request_id: str) -> JSONResponse:
    row = store.get_request(request_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content={"request_id": request_id, "status": "not_found", "failures": [
                {"category": "unknown_request", "message": f"no request {request_id}"}
            ]},
        )
    return JSONResponse(content=row)


@app.get("/requests/{request_id}/summary")
def request_summary(request_id: str) -> JSONResponse:
    if store.get_request(request_id) is None:
        return JSONResponse(status_code=404, content={"request_id": request_id, "status": "not_found"})
    return JSONResponse(content=store.request_summary(request_id))


@app.get("/requests/{request_id}/derivation")
def get_derivation(request_id: str, pred: str, args: str) -> JSONResponse:
    """``args`` is a JSON array, e.g. ``?pred=ancestor&args=%5B%22ann%22,%22cy%22%5D``."""
    import json

    try:
        values = tuple(json.loads(args))
    except (ValueError, TypeError):
        return JSONResponse(status_code=400, content={"failures": [
            {"category": "bad_request", "message": "args must be a JSON array of strings"}
        ]})
    row = store.get_derivation(request_id, pred, values)
    if row is None:
        return JSONResponse(status_code=404, content={"request_id": request_id, "status": "not_found"})
    return JSONResponse(content=row)
