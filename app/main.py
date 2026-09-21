"""CareForce FastAPI application entrypoint."""

from __future__ import annotations

from fastapi import FastAPI

from app.api import directory, plans, tasks

app = FastAPI(
    title="CareForce Scheduling API",
    version="1.0.0",
    description=(
        "Nursing-care task generation, constraint-based assignment and "
        "timeout reassignment.\n\n"
        "**Scope:** scheduling only — no payroll, volunteer or medical "
        "decisions.\n\n"
        "Identity for the demo is supplied via `X-Coordinator-ID` / "
        "`X-Worker-ID` headers. Coordinator actions are restricted to the "
        "units the coordinator is authorised for."
    ),
)

app.include_router(directory.router)
app.include_router(plans.router)
app.include_router(tasks.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
