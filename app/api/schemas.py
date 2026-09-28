"""Pydantic request/response schemas.

Request, response, and error schemas are kept separate per service boundary.
Numeric bounds are exchanged as *strings* so decimal literals from the client
never suffer an unintended binary-float round trip before certification.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Request limits mirror the core config ceilings but are validated up front.
MIN_PRECISION = 10
MAX_PRECISION = 200
MIN_SAMPLES = 3
MAX_SAMPLES = 20_001


class CertifyRequest(BaseModel):
    expression: str = Field(
        ..., min_length=1, description="C^1 expression in the restricted grammar"
    )
    lower: str = Field(..., description="search interval lower bound (decimal)")
    upper: str = Field(..., description="search interval upper bound (decimal)")
    include_approximation: bool = Field(
        default=True,
        description="also run the independent, unverified SciPy approximation",
    )
    sample_count: int = Field(default=1001, ge=MIN_SAMPLES, le=MAX_SAMPLES)
    precision_dps: int | None = Field(
        default=None, ge=MIN_PRECISION, le=MAX_PRECISION
    )
    target_width: str | None = Field(
        default=None, description="certified-enclosure stopping width, e.g. 1e-25"
    )
    max_depth: int | None = Field(default=None, ge=1, le=200)
    max_evals: int | None = Field(default=None, ge=10, le=2_000_000)


class IntervalDTO(BaseModel):
    lower: str
    upper: str


class CertificationDTO(BaseModel):
    theorem: str
    status: str
    newton_iterations: int


class CertifiedRootDTO(BaseModel):
    enclosure: IntervalDTO
    midpoint: str
    certification: CertificationDTO
    evidence: dict[str, Any]
    residual_range_enclosure: IntervalDTO
    derivative_range_enclosure: IntervalDTO


class UndecidedRegionDTO(BaseModel):
    interval: IntervalDTO
    reason: str
    depth: int
    value_range_enclosure: IntervalDTO
    derivative_range_enclosure: IntervalDTO | None = None


class ApproximateRootDTO(BaseModel):
    value: str
    residual: str
    method: str


class ApproximationDTO(BaseModel):
    status: str
    warning: str
    sample_count: int
    convergence_failures: int
    roots: list[ApproximateRootDTO]


class SummaryDTO(BaseModel):
    certified_root_count: int
    undecided_region_count: int
    root_free: bool
    excluded_leaves: int
    bisections: int
    newton_steps: int
    value_evaluations: int
    derivative_evaluations: int
    budget_hit: str | None


class TraceEventDTO(BaseModel):
    seq: int
    elapsed_ms: float
    stage: str
    reason: str
    state: dict[str, Any]


class TraceDTO(BaseModel):
    run_id: str
    elapsed_ms: float
    counters: dict[str, int]
    events_returned: int
    events_on_disk: int
    log_path: str | None
    events: list[TraceEventDTO]


class CertifyResponse(BaseModel):
    expression: str
    search_interval: IntervalDTO
    status: str
    certified_roots: list[CertifiedRootDTO]
    undecided_regions: list[UndecidedRegionDTO]
    approximate_roots_unverified: ApproximationDTO | None = None
    summary: SummaryDTO
    trace: TraceDTO


class ErrorResponse(BaseModel):
    error: dict[str, Any]
