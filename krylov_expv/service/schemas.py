"""Request/response schemas for the expv HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CooMatrix(BaseModel):
    n: int = Field(..., description="matrix dimension (square n x n)")
    row: list[int]
    col: list[int]
    data: list[float]


class ExpvRequest(BaseModel):
    request_id: str | None = Field(
        default=None, description="client correlation id; a uuid is generated when absent"
    )
    matrix: CooMatrix
    vector: list[float]
    t: float = Field(..., description="time horizon; 0 and negative values are legal")
    tol: float | None = Field(default=None, description="relative error tolerance in (0, 1)")
    m_max: int | None = Field(default=None, ge=1, le=200, description="Krylov dimension cap")
    max_steps: int | None = Field(default=None, ge=1, le=100000)


class StepOut(BaseModel):
    index: int
    t_before: float
    tau: float
    krylov_dim: int
    subspace_residual: float
    error_estimate: float
    halvings: int
    happy_breakdown: bool


class FailureOut(BaseModel):
    category: str
    message: str


class ExpvResponse(BaseModel):
    request_id: str
    status: str = Field(..., description="converged | not_converged | rejected")
    w: list[float] | None
    total_error_estimate: float
    max_subspace_residual: float
    num_steps: int
    memory_bytes_used: int
    elapsed_ms: float
    versions: dict[str, str]
    steps: list[StepOut]
    failure: FailureOut | None = None
