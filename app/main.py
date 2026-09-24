"""FastAPI application entry point.

Run locally::

    python3 -m uvicorn app.main:app --reload
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import router
from .executor import ExecutorError

app = FastAPI(
    title="Workload Drain Planner",
    version="1.0.0",
    description=(
        "Local snapshot-based node drain planner and simulator. "
        "All execution happens against an in-memory cluster."
    ),
)
app.include_router(router, prefix="/api/v1")


@app.exception_handler(ExecutorError)
async def _executor_error_handler(request: Request, exc: ExecutorError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": str(exc)}},
    )


@app.get("/")
def root() -> dict:
    return {
        "service": "workload-drain-planner",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/api/v1/healthz",
    }
