from __future__ import annotations

from fastapi import FastAPI

from .routers import alerts, auth, baselines, events

app = FastAPI(
    title="Sentinel",
    version="1.0.0",
    description=(
        "Insider-threat detection service: batch event ingestion with "
        "idempotent de-duplication, streaming anomaly rules, 14-day z-score "
        "baselines and versioned alert triage. PostgreSQL only; no agents."
    ),
)


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(events.router)
app.include_router(alerts.router)
app.include_router(baselines.router)
