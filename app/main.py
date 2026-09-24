"""FastAPI service exposing SO(3) rotation averaging over HTTP."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.rotation import average_rotations

app = FastAPI(title="SO(3) Rotation Averaging", version="1.0.0")


class AverageOptions(BaseModel):
    max_iterations: int = Field(default=100, ge=1, le=10000)
    tolerance: float = Field(default=1e-12, gt=0.0)
    huber_delta: float = Field(
        default=0.5, gt=0.0,
        description="Huber cut-off on geodesic residuals, radians",
    )


class AverageRequest(BaseModel):
    quaternions: list[list[float]] = Field(
        ..., min_length=1,
        description="Unit quaternions as [w, x, y, z]; q and -q are equivalent",
    )
    weights: list[float] | None = Field(
        default=None, description="Optional positive weights, same length as quaternions",
    )
    options: AverageOptions = AverageOptions()


class AverageResponse(BaseModel):
    converged: bool
    iterations: int
    quaternion: list[float]
    rotation_matrix: list[list[float]]
    geodesic_residuals_rad: list[float]
    robust_weights: list[float]
    outlier_indices: list[int]
    multi_solution_hint: bool
    eigen_gap: float
    messages: list[str]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/average", response_model=AverageResponse)
def average(req: AverageRequest) -> AverageResponse:
    try:
        result = average_rotations(
            req.quaternions,
            req.weights,
            max_iterations=req.options.max_iterations,
            tolerance=req.options.tolerance,
            huber_delta=req.options.huber_delta,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return AverageResponse(
        converged=result.converged,
        iterations=result.iterations,
        quaternion=result.quaternion.tolist(),
        rotation_matrix=result.rotation_matrix.tolist(),
        geodesic_residuals_rad=result.residuals_rad.tolist(),
        robust_weights=result.robust_weights.tolist(),
        outlier_indices=result.outlier_indices,
        multi_solution_hint=result.multi_solution_hint,
        eigen_gap=result.eigen_gap,
        messages=result.messages,
    )
