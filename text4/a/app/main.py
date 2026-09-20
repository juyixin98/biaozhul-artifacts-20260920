from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import AppError
from .routers import clock_admin, enrollments, programs, progress, users

app = FastAPI(
    title="SkillPulse",
    version="1.0.0",
    description="Training execution backend: immutable program versions, seat "
    "races, waitlist promotion, step gating and single-certificate issuance.",
)


@app.exception_handler(AppError)
def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": str(exc)}},
    )


app.include_router(users.router)
app.include_router(programs.router)
app.include_router(enrollments.router)
app.include_router(progress.router)
app.include_router(clock_admin.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}
