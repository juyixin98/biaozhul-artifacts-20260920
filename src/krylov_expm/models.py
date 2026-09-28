"""Request/response schemas for the exp(tA)v service."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class CooMatrix(BaseModel):
    """Sparse square matrix in COO form: data[k] at (row[k], col[k])."""

    shape: list[int]
    row: list[int]
    col: list[int]
    data: list[float]


class ExpmvRequest(BaseModel):
    matrix: CooMatrix
    vector: list[float]
    t: float
    tol: Optional[float] = None
    request_id: Optional[str] = None


class FailureInfo(BaseModel):
    category: str
    message: str
    details: dict = Field(default_factory=dict)


class ExpmvResponse(BaseModel):
    request_id: str
    status: str  # "converged" | "not_converged" | "rejected"
    vector: Optional[list[float]] = None
    vector_norm: Optional[float] = None
    evidence: Optional[dict] = None
    failure: Optional[FailureInfo] = None
    meta: dict = Field(default_factory=dict)
