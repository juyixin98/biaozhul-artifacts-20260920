"""Pydantic request/response schemas for the HTTP interface.

The wire format stays sparse: matrices travel as COO triples of the lower
triangle; dense ``n*n`` payloads are never required.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

OrderingName = Literal["natural", "rcm"]


class COOEntry(BaseModel):
    row: int = Field(..., ge=0)
    col: int = Field(..., ge=0)
    value: float


class MatrixInput(BaseModel):
    """Lower-triangular COO description of a symmetric matrix."""

    n: int = Field(..., gt=0, le=20_000)
    entries: list[COOEntry] = Field(default_factory=list)

    @field_validator("entries")
    @classmethod
    def _lower_triangle_only(cls, entries: list[COOEntry]) -> list[COOEntry]:
        for e in entries:
            if e.row < e.col:
                raise ValueError(
                    f"entry ({e.row}, {e.col}) is above the diagonal; "
                    "supply only row >= col"
                )
        return entries


class SolveRequest(BaseModel):
    matrix: MatrixInput
    rhs: list[float] | None = Field(
        default=None, description="single right-hand side of length n"
    )
    ordering: OrderingName = "natural"
    run_id: str | None = Field(
        default=None,
        max_length=80,
        description="optional caller-chosen correlation id",
    )


class SymbolicRequest(BaseModel):
    matrix: MatrixInput
    ordering: OrderingName = "natural"
    run_id: str | None = None


class FillReport(BaseModel):
    nnz_a_lower: int
    nnz_l: int
    fill_in: int
    fill_ratio: float
    etree_height: int
    bandwidth_before: int
    bandwidth_after: int
    ordering: str


class ErrorResponse(BaseModel):
    success: bool = False
    run_id: str
    error_type: str
    message: str
    pivot_index: int | None = None
    pivot_value: float | None = None
    original_pivot_index: int | None = None
