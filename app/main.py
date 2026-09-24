"""FastAPI application exposing the offline trajectory evaluation service."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .errors import EvaluationError
from .integrity import maybe_hmac, response_hash, sha256_hex
from .pipeline import evaluate
from .schemas import EvaluateRequest

app = FastAPI(
    title="Trajectory Error Evaluation API",
    version="1.0.0",
    description=(
        "Offline ATE/RPE evaluation of an estimated trajectory against ground "
        "truth: time-window matching, rigid/Sim(3) alignment, geodesic rotation "
        "errors, explicit failure reporting and response integrity hashes."
    ),
)


def _error(status: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    payload: dict[str, Any] = {"status": "ok", "service": "trajectory-eval", "version": "1.0.0"}
    return JSONResponse(
        content={**payload, "integrity": {"response_sha256": response_hash(payload)}}
    )


@app.post("/api/v1/evaluate")
async def post_evaluate(request: Request) -> JSONResponse:
    raw = await request.body()
    request_sha = sha256_hex(raw)

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _error(400, "INVALID_JSON", "Request body is not valid UTF-8 JSON.", {"reason": str(exc)})
    if not isinstance(data, dict):
        return _error(422, "INVALID_REQUEST", "Request body must be a JSON object.")

    try:
        req = EvaluateRequest.model_validate(data)
    except ValidationError as exc:
        return _error(
            422,
            "INVALID_REQUEST",
            "Request payload failed schema validation.",
            {"errors": exc.errors(include_url=False)},
        )

    try:
        result = evaluate(req)
    except EvaluationError as exc:
        return _error(422, exc.code, exc.message, exc.details)

    # The checksum covers the result payload only; the integrity block is added
    # afterwards so clients can recompute it by hashing the canonical JSON of
    # the response with "integrity" removed.
    integrity: dict[str, Any] = {
        "request_sha256": request_sha,
        "response_sha256": response_hash(result),
    }
    sig = maybe_hmac(result)
    if sig is not None:
        integrity["hmac_sha256"] = sig
        integrity["hmac_note"] = (
            "HMAC-SHA256 over the canonical result JSON, keyed by "
            "TRAJECTORY_EVAL_HMAC_KEY."
        )

    return JSONResponse(content={**result, "integrity": integrity})
