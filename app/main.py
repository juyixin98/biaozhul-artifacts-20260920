"""FastAPI application: upload immutable snapshots and run reachability queries."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .analyzer import UnsupportedProtocolError, ValidationError
from .models import QueryRequest
from .storage import store

app = FastAPI(
    title="Offline NetworkPolicy Reachability Analyzer",
    version=__version__,
    description=(
        "Computes whether traffic between two endpoints is allowed by a set of "
        "Kubernetes NetworkPolicies. Egress and ingress are evaluated "
        "separately; both sides must allow the traffic."
    ),
)


@app.exception_handler(ValidationError)
async def _validation_error_handler(_request: Request, exc: ValidationError):
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "detail": str(exc)},
    )


@app.exception_handler(UnsupportedProtocolError)
async def _unsupported_protocol_handler(
    _request: Request, exc: UnsupportedProtocolError
):
    return JSONResponse(
        status_code=422,
        content={"error": "unsupported_protocol", "detail": str(exc)},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


async def _read_json_object(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """Read a JSON object body, returning a 4xx response on bad input."""
    try:
        raw = await request.json()
    except Exception:
        return None, JSONResponse(
            status_code=400,
            content={"error": "bad_request", "detail": "request body must be JSON"},
        )
    if not isinstance(raw, dict):
        return None, JSONResponse(
            status_code=422,
            content={"error": "validation_error", "detail": "body must be an object"},
        )
    return raw, None


@app.post("/api/v1/snapshots")
async def create_snapshot(request: Request):
    raw, bad = await _read_json_object(request)
    if bad is not None:
        return bad
    try:
        sid, analyzer, created = store.put(raw)
    except Exception as exc:
        # pydantic ValidationError groups messages; expose them verbatim.
        detail = _flatten_error(exc)
        return JSONResponse(
            status_code=422,
            content={"error": "validation_error", "detail": detail},
        )
    return {
        "snapshotId": sid,
        "created": created,
        "warnings": list(analyzer.warnings),
    }


@app.get("/api/v1/snapshots/{sid}")
async def get_snapshot(sid: str) -> dict[str, Any]:
    try:
        analyzer = store.get(sid)
    except KeyError:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": sid}
        )
    snap = analyzer.snapshot
    return {
        "snapshotId": sid,
        "namespaces": len(snap.namespaces),
        "pods": len(snap.pods),
        "policies": len(snap.policies),
        "warnings": list(analyzer.warnings),
    }


@app.get("/api/v1/snapshots")
async def list_snapshots() -> dict[str, Any]:
    return {"snapshotIds": store.list_ids()}


@app.post("/api/v1/snapshots/{sid}/analyze")
async def analyze(sid: str, request: Request) -> JSONResponse:
    try:
        analyzer = store.get(sid)
    except KeyError:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": sid}
        )
    raw, bad = await _read_json_object(request)
    if bad is not None:
        return bad
    try:
        query = QueryRequest.model_validate(raw)
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            content={"error": "validation_error", "detail": _flatten_error(exc)},
        )
    try:
        result = analyzer.analyze(query)
    except UnsupportedProtocolError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": "unsupported_protocol", "detail": str(exc)},
        )
    except ValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": "validation_error", "detail": str(exc)},
        )
    return JSONResponse(content=result)


def _flatten_error(exc: Exception) -> Any:
    """Turn pydantic-style validation errors into a concise structure."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            return [
                {
                    "location": list(e.get("loc", [])),
                    "message": e.get("msg", ""),
                    "type": e.get("type", ""),
                }
                for e in errors()
            ]
        except Exception:
            pass
    return str(exc)
