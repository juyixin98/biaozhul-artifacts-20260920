"""FastAPI application: offline IMU stationary detection & gyro bias service."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .errors import InvalidInputError
from .models import BiasEstimateRequest, EstimateResponse
from .pipeline import run
from . import __version__

app = FastAPI(
    title="IMU Bias Estimator",
    version=__version__,
    description=(
        "Offline stationary-segment detection and robust gyroscope bias "
        "estimation. Pure backend, stateless. All internal math in SI units."
    ),
)


@app.exception_handler(InvalidInputError)
async def invalid_input_handler(_request, exc: InvalidInputError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "service": "imu-bias-estimator", "version": __version__}


@app.get("/api/v1/observability", tags=["meta"])
def observability():
    """Static statement of what static data can and cannot identify."""
    from .pipeline import OBSERVABILITY

    return {
        k: {"observable": v[0], "detail": v[1]} for k, v in OBSERVABILITY.items()
    }


@app.post("/api/v1/estimate", response_model=EstimateResponse, tags=["estimate"])
def estimate(req: BiasEstimateRequest):
    return run(req)
