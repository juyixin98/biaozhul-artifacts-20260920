"""FastAPI application: offline topology scheduling candidate analysis."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .analyzer import FEATURES, SCOPE, analyze
from .crypto import HMAC_ALGORITHM, signed_result, verify
from .filters import FilterError
from .models import AnalyzeRequest, CamelModel

app = FastAPI(
    title="Topology Scheduling Candidate Analysis",
    version=__version__,
    description=(
        "Offline analysis of a Kubernetes scheduler subset: hard-constraint "
        "filtering followed by soft scoring, with per-node reasons and a real "
        "HMAC-SHA256 response signature. Does not connect to any cluster."
    ),
)


class VerifyRequest(CamelModel):
    payload: dict[str, Any]
    signature: str


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/api/v1/info")
def info() -> dict:
    return {
        "scope": SCOPE,
        "version": __version__,
        "implementedFeatures": FEATURES,
        "hmacAlgorithm": HMAC_ALGORITHM,
    }


@app.post("/api/v1/analyze")
async def analyze_endpoint(request: Request) -> JSONResponse:
    raw = await request.json()
    try:
        parsed = AnalyzeRequest.model_validate(raw)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"error": "ValidationError", "detail": exc.errors()})
    try:
        result = analyze(parsed)
    except FilterError as exc:
        return JSONResponse(status_code=422, content={"error": "InvalidQuantity", "detail": str(exc)})
    return JSONResponse(content=signed_result(result))


@app.post("/api/v1/verify-signature")
def verify_signature(body: VerifyRequest) -> dict:
    valid = verify(body.payload, body.signature)
    return {"valid": valid, "hmacAlgorithm": HMAC_ALGORITHM}
