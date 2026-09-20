"""ConsentVault FastAPI application.

The service records *attributable consent state* — a faithful, auditable log of
grants, withdrawals and expirations. It does NOT by itself certify compliance
with any specific regulation (GDPR/ePrivacy/CCPA/...); legal conformance
depends on how an organization operates the system and the policies it
publishes.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import ConsentError
from app.routers import admin, consents, policies

app = FastAPI(
    title="ConsentVault",
    version="1.0.0",
    description=(
        "Attributable consent-record backend with an immutable event ledger. "
        "Tracks grant/withdraw/expiry state; does not certify regulatory compliance."
    ),
)

app.include_router(consents.router)
app.include_router(policies.router)
app.include_router(admin.router)


@app.exception_handler(ConsentError)
async def consent_error_handler(request: Request, exc: ConsentError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}
