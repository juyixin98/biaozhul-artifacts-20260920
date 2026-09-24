"""FastAPI HTTP interface for the offline sparse-trajectory smoother.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .smoother import Corridor, smooth_trajectory

app = FastAPI(
    title="Sparse Trajectory Smoother",
    version="1.0.0",
    description=(
        "Offline path smoothing: minimises observation deviation plus a "
        "second-difference smoothness term, subject to per-point convex "
        "corridor constraints and fixed endpoints. Returns feasibility and "
        "KKT (optimality) residuals."
    ),
)


# --------------------------------------------------------------------------
# Request / response models
# --------------------------------------------------------------------------

class CorridorModel(BaseModel):
    """Polytope { y : A y <= b } constraining one trajectory point."""

    A: list[list[float]] = Field(default_factory=list, description="m_i x d matrix")
    b: list[float] = Field(default_factory=list, description="m_i right-hand side")


class SmoothRequest(BaseModel):
    observations: list[list[float]] = Field(
        ..., description="n observed positions, each of dimension d (n >= 2)"
    )
    corridors: list[CorridorModel] | None = Field(
        default=None,
        description="One corridor per point (length n); omit for an unconstrained problem",
    )
    smooth_weight: float = Field(default=1.0, ge=0.0, description="lambda on the second-difference term")
    obs_weights: list[float] | float = Field(
        default=1.0, description="Per-point observation weights (all > 0) or a scalar"
    )
    start: list[float] | None = Field(default=None, description="Fixed first point; defaults to observations[0]")
    end: list[float] | None = Field(default=None, description="Fixed last point; defaults to observations[-1]")


class ResidualsModel(BaseModel):
    primal_infeasibility: float
    stationarity: float
    complementary_slackness: float
    dual_infeasibility: float


class SmoothResponse(BaseModel):
    status: str  # "optimal" | "infeasible" | "iteration_limit" | "numerical_error"
    feasible: bool
    path: list[list[float]] | None
    objective: float | None
    residuals: ResidualsModel | None
    iterations: int
    message: str


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------

def _validate(req: SmoothRequest) -> tuple[np.ndarray, list[Corridor], np.ndarray, np.ndarray, np.ndarray]:
    z = np.asarray(req.observations, dtype=float)
    if z.ndim != 2 or z.shape[0] < 2:
        raise HTTPException(422, "observations must be an n x d array with n >= 2")
    n, d = z.shape
    if not np.all(np.isfinite(z)):
        raise HTTPException(422, "observations contain NaN or inf")

    if req.corridors is None:
        corridors = [Corridor(np.zeros((0, d)), np.zeros(0)) for _ in range(n)]
    else:
        if len(req.corridors) != n:
            raise HTTPException(422, f"corridors must have length n={n}, got {len(req.corridors)}")
        corridors = []
        for i, c in enumerate(req.corridors):
            A = np.asarray(c.A, dtype=float).reshape(-1, d) if c.A else np.zeros((0, d))
            b = np.asarray(c.b, dtype=float).reshape(-1)
            if A.shape[1] != d:
                raise HTTPException(422, f"corridor {i}: A rows must have length d={d}")
            if A.shape[0] != b.shape[0]:
                raise HTTPException(422, f"corridor {i}: len(b)={b.shape[0]} != rows(A)={A.shape[0]}")
            if not (np.all(np.isfinite(A)) and np.all(np.isfinite(b))):
                raise HTTPException(422, f"corridor {i}: NaN or inf in A or b")
            corridors.append(Corridor(A, b))

    if isinstance(req.obs_weights, (int, float)):
        w = np.full(n, float(req.obs_weights))
    else:
        w = np.asarray(req.obs_weights, dtype=float)
        if w.shape != (n,):
            raise HTTPException(422, f"obs_weights must be a scalar or have length n={n}")
    if not np.all(np.isfinite(w)) or np.any(w <= 0):
        raise HTTPException(422, "obs_weights must be finite and strictly positive")

    start = np.asarray(req.start if req.start is not None else z[0], dtype=float)
    end = np.asarray(req.end if req.end is not None else z[-1], dtype=float)
    if start.shape != (d,) or end.shape != (d,):
        raise HTTPException(422, f"start and end must have dimension d={d}")
    if not (np.all(np.isfinite(start)) and np.all(np.isfinite(end))):
        raise HTTPException(422, "start/end contain NaN or inf")

    return z, corridors, w, start, end


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/smooth", response_model=SmoothResponse)
def smooth(req: SmoothRequest) -> SmoothResponse:
    z, corridors, w, start, end = _validate(req)
    try:
        result = smooth_trajectory(
            observations=z,
            corridors=corridors,
            smooth_weight=req.smooth_weight,
            obs_weights=w,
            start=start,
            end=end,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return SmoothResponse(
            status="numerical_error",
            feasible=False,
            path=None,
            objective=None,
            residuals=None,
            iterations=0,
            message=f"{type(exc).__name__}: {exc}",
        )

    return SmoothResponse(
        status=result.status,
        feasible=result.feasible,
        path=result.path.tolist() if result.path is not None else None,
        objective=result.objective,
        residuals=ResidualsModel(**result.residuals) if result.residuals else None,
        iterations=result.iterations,
        message=result.message,
    )
