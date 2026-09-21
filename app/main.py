from fastapi import FastAPI

from app.routers import alerts, auth, baselines, events

app = FastAPI(
    title="Sentinel",
    description="Local employee-activity anomaly detection: batch ingestion, "
    "event-time rules, z-score baselines, scoped alert triage.",
    version="1.0.0",
)

app.include_router(auth.router)
app.include_router(events.router)
app.include_router(alerts.router)
app.include_router(baselines.router)


@app.get("/health")
def health():
    return {"status": "ok"}
