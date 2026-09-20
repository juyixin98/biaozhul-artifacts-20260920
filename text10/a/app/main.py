from fastapi import FastAPI

from .errors import register_exception_handlers
from .routers import budgets, journals, periods, reports

app = FastAPI(
    title="CivicLedger",
    version="0.1.0",
    description=(
        "Municipal fund-accounting backend: journal entries, budget "
        "encumbrance, and period close. Not a certified accounting product."
    ),
)

register_exception_handlers(app)

app.include_router(journals.router)
app.include_router(budgets.router)
app.include_router(periods.router)
app.include_router(reports.router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
