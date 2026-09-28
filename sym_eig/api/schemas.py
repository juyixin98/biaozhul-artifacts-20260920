"""Pydantic request/response models for the HTTP interface."""

from typing import Any, Literal
from pydantic import BaseModel, Field


class EigenRequest(BaseModel):
    # Deliberately untyped element-wise: pydantic must NOT coerce bools,
    # ints-as-strings, etc. before the service boundary validator runs, so
    # that every malformed matrix gets the same classified error envelope
    # (sym_eig.numerical.validation.to_finite_matrix).
    matrix: list[list[Any]] = Field(
        ..., description="Real symmetric matrix as a row-major nested array."
    )
    request_id: str | None = Field(
        default=None,
        description="Client correlation id; echoed back. A UUID is "
                    "generated when omitted.",
    )
    max_iters: int | None = Field(
        default=None,
        ge=1,
        description="Per-block implicit-QR sweep budget (iteration budget). "
                    "Defaults to the server setting.",
    )
    symmetry_rtol: float | None = Field(default=None, gt=0.0)
    symmetry_atol: float | None = Field(default=None, ge=0.0)
    residual_rtol: float | None = Field(default=None, gt=0.0)
    orthogonality_tol: float | None = Field(default=None, gt=0.0)
    reconstruction_rtol: float | None = Field(default=None, gt=0.0)
    cluster_rtol: float | None = Field(default=None, gt=0.0)
    reference: Literal["auto", "mpmath", "scipy", "none"] = "auto"
    include_vectors: bool = True


class GateOut(BaseModel):
    name: str
    value: float
    threshold: float
    passed: bool
    detail: str
    status: str = "PASS"


class EigenResponseOut(BaseModel):
    request_id: str
    verdict: Literal["SUCCESS", "UNCERTAIN", "FAILED"]
    dimension: int
    eigenvalues: list[float] | None = None
    eigenvectors: list[list[float]] | None = None
    error_category: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] = Field(default_factory=dict)
    uncertainties: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(
        default_factory=list,
        description="Non-fatal conditioning conclusions: backward-stable "
                    "result whose absolute precision is bounded by the input "
                    "data, reported separately from uncertainties.",
    )
    gates: list[GateOut] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    clusters: list[dict[str, Any]] = Field(default_factory=list)
    reference_comparison: dict[str, Any] | None = None
    computation: dict[str, Any] = Field(default_factory=dict)
    effective_config: dict[str, Any] = Field(default_factory=dict)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    processing_location: dict[str, Any] = Field(default_factory=dict)
    algorithm: str
    service_version: str


class HealthOut(BaseModel):
    status: str
    service_version: str
    algorithm: str
    config: dict[str, Any]
