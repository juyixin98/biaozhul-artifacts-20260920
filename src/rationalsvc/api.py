"""FastAPI service interface.

Endpoints
---------
GET  /health                        liveness + backend description
POST /solve                         exact solve/rank of one system
POST /rank                          rank-only convenience endpoint
GET  /runs/{run_id}                 replay a past run from the JSONL log

Error envelope (every non-2xx response)::

    {"error": "<ErrorCode>", "message": str, "details": {...}, "run_id": str}

The four required failure families are distinguishable by ``error``:
``input_*`` (400), ``state_conflict`` (409), ``budget_exhausted`` (422,
carrying ``details.progress``), ``computation_failed`` (500).
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, numeric_input, runner
from .errors import ErrorCode, ServiceError

RUN_LOG_PATH = os.environ.get("RATIONALSVC_RUN_LOG", "run_log.jsonl")

app = FastAPI(
    title="Exact Rational Matrix Service",
    version=__version__,
    description=(
        "Exact linear-equation solving and matrix rank over the rationals via "
        "fraction-free Bareiss elimination, with digit budgets, parametric "
        "solutions, inconsistency witnesses and replayable run logs."
    ),
)


def _logger() -> runner.RunLogger:
    return runner.RunLogger(RUN_LOG_PATH)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "exact_backend": "Python integers + fractions.Fraction",
        "approximate_diagnosis": "numpy / scipy / mpmath (labelled, advisory only)",
        "run_log": RUN_LOG_PATH,
    }


@app.post("/solve")
async def solve(request: Request) -> JSONResponse:
    return await _dispatch(request)


@app.post("/rank")
async def rank_only(request: Request) -> JSONResponse:
    return await _dispatch(request, force_want="rank")


async def _dispatch(request: Request, force_want: str | None = None) -> JSONResponse:
    run_id = runner.new_run_id()
    logger = _logger()
    try:
        payload = await request.json()
    except Exception as exc:  # JSON decode error at the boundary
        from .errors import InputMalformed

        err = InputMalformed("request body is not valid JSON",
                             {"reason": str(exc)})
        runner.log_error(logger, run_id, err.category.value, err.message, err.details)
        return _error_response(err, run_id, http_status=400)

    try:
        parsed = numeric_input.parse_request(payload)
        if force_want == "rank":
            import dataclasses

            parsed = dataclasses.replace(parsed, want="rank")
        result = runner.solve_system(parsed, logger, run_id=run_id)
        return JSONResponse(result)
    except ServiceError as exc:
        runner.log_error(logger, run_id, exc.category.value, exc.message, exc.details)
        return _error_response(exc, run_id, http_status=exc.http_status)
    except Exception as exc:  # defensive: never leak a stack trace as 200
        from .errors import ComputationFailed

        ce = ComputationFailed("unexpected internal failure",
                               {"exception_type": type(exc).__name__,
                                "reason": str(exc)})
        runner.log_error(logger, run_id, ce.category.value, ce.message, ce.details)
        return _error_response(ce, run_id, http_status=500)


def _error_response(exc: ServiceError, run_id: str, http_status: int) -> JSONResponse:
    body: dict[str, Any] = exc.to_dict()
    body["run_id"] = run_id
    return JSONResponse(body, status_code=http_status)


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> JSONResponse:
    """Replay: return all logged events for a run id, newest last."""
    events = _read_run_events(run_id)
    if not events:
        return JSONResponse(
            {"error": ErrorCode.INPUT_EMPTY.value,
             "message": f"no run found with id {run_id!r}",
             "details": {"run_log": RUN_LOG_PATH},
             "run_id": run_id},
            status_code=404,
        )
    summary = {
        "run_id": run_id,
        "event_count": len(events),
        "events": events,
    }
    return JSONResponse(summary)


def _read_run_events(run_id: str) -> list[dict]:
    import json

    if not os.path.exists(RUN_LOG_PATH):
        return []
    out: list[dict] = []
    with open(RUN_LOG_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("run_id") == run_id:
                out.append(rec)
    return out
