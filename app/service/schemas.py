"""Pydantic request/response schemas for the HTTP service."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

OrderingName = Literal["natural", "minimum_degree", "nested_dissection"]


class CooMatrix(BaseModel):
    n: int = Field(..., gt=0, description="matrix dimension")
    rows: list[int]
    cols: list[int]
    values: list[float]


class SolveRequest(BaseModel):
    matrix: CooMatrix
    rhs: list[float]
    ordering: OrderingName = "minimum_degree"
    with_mpmath: bool = True


class FactorRequest(BaseModel):
    matrix: CooMatrix
    ordering: OrderingName = "minimum_degree"


class OrderingCompareRequest(BaseModel):
    matrix: CooMatrix
    methods: list[OrderingName] = Field(
        default_factory=lambda: ["natural", "minimum_degree",
                                 "nested_dissection"])


class EvidenceOut(BaseModel):
    residual_abs: float
    residual_rel: float
    reconstruction_abs: float
    dense_solution_error: float | None
    mpmath_solution_error: float | None
    fill_ratio: float
    passed: bool
    reasons: list[str]


class ReportOut(BaseModel):
    run_id: str
    n: int
    ordering: str
    nnz_input: int
    nnz_orig_lower: int
    nnz_l: int
    fill_entries: int
    fill_ratio: float
    elapsed_symbolic: float
    elapsed_numeric: float
    elapsed_total: float
    pivot_count: int
    pattern_reused: bool
    pattern_fingerprint: str
    ordering_fill_est: int


class SolveResponse(BaseModel):
    status: Literal["ok"] = "ok"
    run_id: str
    solution: list[float]
    report: ReportOut
    evidence: EvidenceOut


class FactorResponse(BaseModel):
    status: Literal["ok"] = "ok"
    run_id: str
    diag_d: list[float]
    report: ReportOut


class ErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    run_id: str
    error_code: str
    error_category: str
    message: str
    details: dict
