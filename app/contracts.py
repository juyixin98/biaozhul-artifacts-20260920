"""Statistical contract: request/response schemas and declared assumptions.

The contract makes the distinction the task requires explicit:

* **Instrument relevance** (rank / non-weak first stage) is *testable* from the
  data and is checked.
* **Exclusion / exogeneity** (``E[Z'e]=0``) is an *assumption supplied by the
  caller*. It is NEVER inferred from correlations. It is carried verbatim into
  the result record as a declared (unverified) assumption.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

CovType = Literal["conventional", "robust"]


class AssumptionStatus(str, Enum):
    DECLARED = "declared"          # caller asserts it; not testable from this data
    SUPPORTED = "supported"        # data evidence consistent (never proof)
    REJECTED = "rejected"          # data evidence inconsistent
    NOT_ASSESSED = "not_assessed"


class Verdict(str, Enum):
    ACCEPTED = "accepted"            # identified, instruments not weak
    ACCEPTED_WITH_WARNING = "accepted_with_warning"  # identified but marginal/collinear
    REJECTED = "rejected"            # rank/order condition fails -> not estimable
    INCONCLUSIVE = "inconclusive"    # weak instruments: estimates reported, unreliable


class FailureCategory(str, Enum):
    NONE = "none"
    INVALID_REQUEST = "invalid_request"
    INSUFFICIENT_OBSERVATIONS = "insufficient_observations"
    NON_FINITE_DATA = "non_finite_data"
    SINGULAR_DESIGN = "singular_design"          # Z or W rank deficient
    UNDERIDENTIFIED = "underidentified"          # order condition: L < K
    WEAK_IDENTIFICATION = "weak_identification"  # rank/Cragg-Donald failure
    COLLINEAR_INSTRUMENTS = "collinear_instruments"
    WEAK_INSTRUMENTS = "weak_instruments"        # F / CD below critical value
    INAPPLICABLE_TEST = "inapplicable_test"


class ColumnData(BaseModel):
    """Named column vectors; all arrays must share one length."""

    columns: dict[str, list[float]]

    @field_validator("columns")
    @classmethod
    def _nonempty(cls, v: dict[str, list[float]]) -> dict[str, list[float]]:
        if not v:
            raise ValueError("columns must contain at least one named series")
        return v


class EstimateRequest(BaseModel):
    request_id: str | None = Field(default=None, description="caller correlation id; generated if absent")
    dependent: str = Field(description="name of y column")
    endogenous: list[str] = Field(description="names of endogenous regressors X")
    exogenous: list[str] = Field(default_factory=list, description="names of included exogenous W")
    instruments: list[str] = Field(description="names of excluded instruments Z")
    columns: dict[str, list[float]]
    cov_type: CovType = "conventional"
    add_constant: bool = True
    run_overid: bool = True
    run_endogeneity: bool = True
    # Explicit assumption declaration (NOT inferred):
    assume_exclusion_restriction: bool = Field(
        ...,
        description=(
            "Caller must explicitly declare the exclusion restriction "
            "E[Z'epsilon]=0. Required because it cannot be established from "
            "instrument-outcome correlations."
        ),
    )
    exclusion_rationale: str | None = Field(
        default=None,
        description="Free-text justification for the exclusion restriction (stored, not verified).",
    )

    @field_validator("endogenous")
    @classmethod
    def _endog_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one endogenous regressor is required")
        return v

    @field_validator("instruments")
    @classmethod
    def _inst_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one instrument is required")
        return v


class CoefficientResult(BaseModel):
    name: str
    estimate: float
    std_error: float
    z_stat: float
    p_value: float
    ci_lower: float
    ci_upper: float


class FirstStageResult(BaseModel):
    endogenous_name: str
    partial_f_stat: float
    partial_f_pvalue: float
    partial_r_squared: float
    partial_r_squared_adjusted: float
    coefficients: dict[str, float]


class AssumptionRecord(BaseModel):
    name: str
    status: AssumptionStatus
    detail: str


class DiagnosticResult(BaseModel):
    name: str
    value: float | None = None
    p_value: float | None = None
    status: AssumptionStatus
    detail: str


class DecisionRecord(BaseModel):
    """Why the service accepted / rejected / could not decide."""

    request_id: str
    fingerprint: str
    n_obs: int
    verdict: Verdict
    failure_category: FailureCategory
    reasons: list[str]
    key_state: dict
    assumptions: list[AssumptionRecord]


class EstimateResponse(BaseModel):
    request_id: str
    status: Literal["estimated", "estimated_weak", "failed"]
    n_obs: int
    n_endogenous: int
    n_instruments: int
    n_exogenous: int
    coefficients: list[CoefficientResult]
    first_stage: list[FirstStageResult]
    diagnostics: list[DiagnosticResult]
    decision: DecisionRecord
