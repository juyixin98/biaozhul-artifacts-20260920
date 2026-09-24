"""FastAPI application: offline trajectory error evaluation."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .errors import TrajectoryError
from .evaluation import evaluate
from .schemas import EvalRequest

app = FastAPI(
    title="Trajectory Error Evaluation API",
    version="1.0.0",
    description=(
        "Associate estimated and ground-truth poses by bounded time "
        "difference, align rigidly (SE(3)) or optionally with scale "
        "(Sim(3), explicitly tagged), then report ATE and fixed-span RPE. "
        "Duplicate timestamps, zero matches and degenerate alignments are "
        "hard errors."
    ),
)


@app.exception_handler(TrajectoryError)
async def _trajectory_error_handler(_request, exc: TrajectoryError):
    return JSONResponse(
        status_code=400,
        content={
            "error": True,
            "code": exc.code,
            "message": str(exc),
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/evaluate")
async def evaluate_endpoint(req: EvalRequest):
    return evaluate(req)
