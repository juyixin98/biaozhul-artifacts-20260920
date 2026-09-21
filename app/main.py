from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from .errors import DomainError
from .routers import budgets, journals, periods, reports

app = FastAPI(
    title="CivicLedger",
    version="0.1.0",
    description=(
        "Municipal fund-accounting backend: balanced journal entries, budget "
        "encumbrance, and period close. Scope is deliberately limited — no "
        "payroll, no full procurement suite, and no claim of regulatory "
        "accounting certification."
    ),
)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    return JSONResponse(status_code=409, content={"detail": "integrity constraint violation"})


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(journals.router)
app.include_router(budgets.router)
app.include_router(periods.router)
app.include_router(reports.router)
