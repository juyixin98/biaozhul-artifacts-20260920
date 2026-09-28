"""HTTP API schemas (Pydantic) - the validated service boundary.

Layer 5a. These mirror the tensor types but speak JSON. Malformed JSON shapes
are rejected here with 422; domain validation (index range, dim, finite
values) happens in the core and returns the domain error code with 400.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class GradientBatchIn(BaseModel):
    # ``run_id`` lets a caller correlate logs/results with the exact request.
    run_id: Optional[str] = Field(default=None, description="Caller correlation id")
    batch_id: Optional[str] = Field(default=None, description="Optional batch label")
    indices: List[int] = Field(..., description="Embedding row indices; duplicates are summed")
    values: List[List[float]] = Field(
        ..., description="One gradient row per index, each of length dim"
    )


class BatchResultOut(BaseModel):
    run_id: str
    batch_id: Optional[str]
    verdict: str
    raw_row_count: int
    n_unique: int
    n_active: int
    n_zero_skipped: int
    active_indices: List[int]
    zero_skipped_indices: List[int]
    unique_indices: List[int]
    global_step_before: int
    global_step_after: int
    max_abs_delta: float
    clip: dict


class RowOut(BaseModel):
    index: int
    weight: List[float]
    momentum: List[float]
    row_steps: int
    touched: bool


class SummaryOut(BaseModel):
    num_rows: int
    dim: int
    global_step: int
    n_touched: int
    n_dirty: int
    n_seen: int


class ErrorOut(BaseModel):
    error_code: str
    message: str
    run_id: str
    details: dict


class PersistOut(BaseModel):
    run_id: str
    verdict: str
    persisted_touched_rows: int
    global_step: int
    state_dir: str
