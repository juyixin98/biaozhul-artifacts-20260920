from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from .errors import CivicLedgerError
from .routers import budgets, entries, master_data, periods, reports

app = FastAPI(
    title="CivicLedger",
    version="0.1.0",
    description=(
        "Fund-accounting backend focused on journal entries, budget "
        "pre-occupancy and period close. This software does not claim "
        "compliance with any accounting regulation or certification."
    ),
)


@app.exception_handler(CivicLedgerError)
async def civicledger_error_handler(request: Request, exc: CivicLedgerError):
    body = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details:
        body["error"]["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed",
                "details": exc.errors(),
            }
        },
    )


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    # Last-resort guard for unique/FK races not translated by the service
    # layer. The request transaction is rolled back by the session dependency.
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "conflict",
                "message": "Database constraint violation (duplicate or invalid reference)",
            }
        },
    )


app.include_router(entries.router)
app.include_router(budgets.router)
app.include_router(periods.router)
app.include_router(master_data.router)
app.include_router(reports.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
