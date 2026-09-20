"""ConsentVault FastAPI application entrypoint."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.routers.api import router
from app.services.errors import ServiceError

app = FastAPI(
    title="ConsentVault",
    version="1.0.0",
    description=(
        "Traceable consent-record backend (grant / withdraw / expiry, "
        "immutable policy versions, event-sourced audit history). "
        "This service records and reports consent state; it does not claim "
        "compliance with or certification against any specific regulation."
    ),
)


@app.exception_handler(ServiceError)
async def service_error_handler(_request, exc: ServiceError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": exc.detail},
    )


@app.get("/healthz", tags=["meta"])
def healthz():
    return {"status": "ok"}


app.include_router(router, prefix="/v1")
