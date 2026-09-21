from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import DomainError
from .routers import certificates, courses, enrollments, jobs, programs, progress

app = FastAPI(
    title="SkillPulse Training Execution API",
    description=(
        "Versioned training programs, capacity-limited course enrollment with "
        "waitlists and 48h seat holds, prerequisite-gated step results, and "
        "idempotent certificate issuance."
    ),
    version="1.0.0",
)


@app.exception_handler(DomainError)
def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.include_router(programs.router)
app.include_router(courses.router)
app.include_router(enrollments.router)
app.include_router(progress.router)
app.include_router(certificates.router)
app.include_router(jobs.router)


@app.get("/health")
def health():
    return {"status": "ok"}
