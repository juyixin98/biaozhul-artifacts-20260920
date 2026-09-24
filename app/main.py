"""FastAPI application: offline fixed-priority RTA schedulability service."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .analyzer import analyze, canonical_request_json
from .models import AnalysisRequest

app = FastAPI(
    title="Real-Time Fixed-Priority Schedulability Analysis (RTA)",
    version=__version__,
    description=(
        "Offline response-time analysis for fixed-priority periodic tasks with "
        "constrained deadlines (D_i <= T_i) and a blocking upper bound. "
        "Single preemptive core, independent tasks. Low utilization is never "
        "treated as sufficient; verdicts come solely from the RTA iteration "
        "and are cross-checked against a discrete-event simulation."
    ),
)


def _error(code: str, detail: str, status: int = 422) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": "model_violation", "code": code, "detail": detail},
    )


@app.exception_handler(RequestValidationError)
async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Turn pydantic errors into a readable, single-detail rejection.
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []) if p != "body")
        msg = err.get("msg", "invalid value")
        parts.append(f"{loc or '<root>'}: {msg}" if loc else msg)
    return _error("invalid_input", "; ".join(parts))


@app.exception_handler(ValidationError)
async def handle_pydantic(_: Request, exc: ValidationError) -> JSONResponse:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []))
        parts.append(f"{loc or '<root>'}: {err.get('msg', 'invalid value')}")
    return _error("invalid_input", "; ".join(parts))


@app.get("/", tags=["meta"])
def root() -> Dict[str, Any]:
    return {
        "service": "rta-schedulability",
        "version": __version__,
        "model": (
            "fixed-priority periodic tasks, D_i <= T_i, single preemptive core, "
            "independent tasks; see /docs and /health"
        ),
        "endpoints": ["/health", "/api/v1/analyze", "/api/v1/digest", "/docs"],
    }


@app.get("/health", tags=["meta"])
def health() -> Dict[str, Any]:
    return {"status": "ok", "version": __version__}


@app.post("/api/v1/digest", tags=["crypto"])
def digest_only(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the SHA-256 of a canonicalized JSON payload (utility endpoint).

    Performs a real hash; identical semantic JSON always maps to the same digest
    because object keys are sorted and whitespace is removed before hashing.
    """
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {
        "alg": "SHA-256",
        "canonical_encoding": "json:sort_keys,separators=(',',':')",
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "canonical_payload_bytes": len(canonical),
    }


@app.post("/api/v1/analyze", tags=["analysis"])
async def analyze_endpoint(request: Request) -> JSONResponse:
    """Validate the task set against the analysis model, run RTA + simulation."""
    raw = await request.body()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _error("invalid_json", f"request body is not valid UTF-8 JSON: {exc}", 400)
    if not isinstance(data, dict):
        return _error("invalid_input", "request body must be a JSON object", 422)

    try:
        req = AnalysisRequest.model_validate(data)
    except ValidationError as exc:
        parts = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", []))
            parts.append(f"{loc or '<root>'}: {err.get('msg', 'invalid value')}")
        return _error("invalid_input", "; ".join(parts), 422)

    result = analyze(req)
    # Echo the real request digest at the top level too.
    result["integrity"]["canonical_sha256_of_request"] = result["integrity"]["sha256"]
    return JSONResponse(content=result)
