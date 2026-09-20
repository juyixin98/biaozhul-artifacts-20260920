"""FastAPI application entry point."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import DomainError
from app.routers import admin, auth, enrollments, programs, progress

app = FastAPI(
    title="SkillPulse API",
    version="1.0.0",
    description=(
        "Training execution backend: versioned programs, capped enrolment "
        "with waitlist, 48h seat holds, gated step progression, single "
        "version-bound certificates."
    ),
)

app.include_router(auth.router)
app.include_router(programs.router)
app.include_router(enrollments.router)
app.include_router(progress.router)
app.include_router(admin.router)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}
