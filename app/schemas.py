"""Pydantic request/response schemas.

Matrix entries accept ``float`` or ``str``. Send strings to carry more than
~16 significant decimal digits; JSON numbers are limited to float64 by the
JSON grammar used by Python clients.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MatrixEntry = int | float | str
Matrix = list[list[MatrixEntry]]
Vector = list[MatrixEntry]


class SolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    A: Matrix = Field(..., description="Square coefficient matrix A.")
    B: Matrix | Vector = Field(
        ..., description="Right-hand side: column vector or n x k matrix."
    )
    config: dict[str, Any] | None = Field(
        default=None, description="Optional per-request solver overrides."
    )


class ColumnOutcomeSchema(BaseModel):
    column: int
    status: str
    best_eta: str
    final_omega: str
    forward_bound: str
    reason: str
    accepted_at_stage: str | None
    accepted_at_iteration: int | None


class StageRecordSchema(BaseModel):
    stage: str
    used: bool
    factor_pivot_ratio: str
    iterations: int
    note: str


class StageEventSchema(BaseModel):
    stage: str
    iteration: int
    per_column_eta: list[str]
    note: str


class ConditionSchema(BaseModel):
    method: str
    cond: str
    log10_cond: float | None
    svd_cond: str | None
    estimated_at_dps: int
    digits_lost_estimate: float | None
    attainable_note: str


class ShapeSchema(BaseModel):
    n: int
    nrhs: int


class SolveResponse(BaseModel):
    request_id: str
    status: str
    reason: str
    shape: ShapeSchema
    condition: ConditionSchema
    precision_floor_eta: str
    columns: list[ColumnOutcomeSchema]
    stages: list[StageRecordSchema]
    events: list[StageEventSchema]
    solution: list[list[str]] | None
    elapsed_seconds: float


class ErrorResponse(BaseModel):
    request_id: str
    status: str
    reason: str


class HealthResponse(BaseModel):
    status: str
    version: str
