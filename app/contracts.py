"""Statistical contract: request / response models and failure categories.

The contract deliberately separates *point estimates*, *diagnostics*,
*excluded records* and *uncertain conclusions* so that a failure or an
uncertain assumption is never silently folded into a number.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# Failure categories
# --------------------------------------------------------------------------- #
class FailureCategory(str, Enum):
    """Explicit, machine-readable reasons an estimate is refused/qualified."""

    IDENTITY_MISSING_PERIOD = "identity_missing_period"
    """An object is present in only one period. Missing periods are NOT zero;
    the object is excluded from the balanced sample."""

    DUPLICATE_OBJECT_PERIOD = "duplicate_object_period"
    """More than one row for the same (object, period); identity ambiguous."""

    EMPTY_CELL = "empty_cell"
    """One of the four group x period cells has no observation after balancing."""

    DEGENERATE_DESIGN = "degenerate_design"
    """No treated and/or no control group, or only one distinct period."""

    CONTAMINATED_CONTROL = "contaminated_control"
    """A control object is itself treated in the post period ("处理污染")."""

    PRETREND_TREATED_EARLY = "pretrend_treated_early"
    """A treated object is already treated in the earlier/pre period."""

    PRETREND_REJECTED = "pretrend_rejected"
    """The pre-trend diagnostic rejects parallel trends on the observed window.
    Diagnostic only; it never proves parallel trends when it fails to reject."""

    CLUSTER_VARIANCE_DEGENERATE = "cluster_variance_degenerate"
    """Fewer than two clusters in a group; clustered SE not identified."""

    EVENT_STAGGER_UNSUPPORTED = "event_stagger_unsupported"
    """Staggered timing falls outside the supported model; refused explicitly."""

    INVALID_REQUEST = "invalid_request"
    """Structural/validation failure in the request payload."""


class Severity(str, Enum):
    ERROR = "error"       # estimate refused
    WARNING = "warning"   # estimate returned, but qualified
    INFO = "info"         # diagnostic only


class Failure(BaseModel):
    category: FailureCategory
    severity: Severity
    message: str
    object_ids: list[str] = Field(default_factory=list)
    detail: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class Observation(BaseModel):
    object_id: str = Field(..., min_length=1)
    period: int
    y: float
    # Group membership and treatment timing are supplied as panel attributes.
    treated_group: Optional[bool] = None
    treated_this_period: Optional[bool] = None

    @field_validator("object_id")
    @classmethod
    def _oid_nonblank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("object_id must be non-blank")
        return v


class DIDRequest(BaseModel):
    request_id: str = Field(..., min_length=1, description="Caller-supplied correlation id")
    observations: list[Observation] = Field(..., min_length=1)
    pre_period: int
    post_period: int
    # Optional second earlier (untreated) period enabling a pre-trend placebo.
    earlier_period: Optional[int] = None
    # If False, contaminated controls raise rather than merely warn.
    allow_contaminated_controls: bool = True
    include_reference_regression: bool = True


class EventStudyRequest(BaseModel):
    request_id: str = Field(..., min_length=1)
    observations: list[Observation] = Field(..., min_length=1)
    # Event-time horizon, e.g. -2..2 relative to each object's first treatment.
    min_event_time: int = -2
    max_event_time: int = 2


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #
class CellMean(BaseModel):
    group: str           # "treated" | "control"
    period: int
    n_objects: int
    mean: float
    sum_weights: float


class Decomposition(BaseModel):
    """Hand-checkable 2x2 difference-in-differences bookkeeping."""

    treated_pre: CellMean
    treated_post: CellMean
    control_pre: CellMean
    control_post: CellMean
    treated_change: float       # post - pre
    control_change: float       # post - pre
    did: float                  # treated_change - control_change


class ExcludedRecord(BaseModel):
    object_id: str
    reasons: list[FailureCategory]
    periods_present: list[int]
    detail: dict = Field(default_factory=dict)


class ClusteredSE(BaseModel):
    method: str
    did_standard_error: float
    t_stat: float
    p_value: float
    ci95_low: float
    ci95_high: float
    n_objects: int
    n_clusters: int
    df: int


class ReferenceRegression(BaseModel):
    """Independent cross-check produced by a separate closed-form OLS path."""

    model: str
    did_coefficient: float
    standard_error: float
    p_value: float
    coefficients: dict[str, float]
    max_abs_did_discrepancy: float


class PretrendDiagnostic(BaseModel):
    feasible: bool
    pretrend_difference: Optional[float] = None
    standard_error: Optional[float] = None
    t_stat: Optional[float] = None
    p_value: Optional[float] = None
    conclusion: str
    note: str


class ContaminationReport(BaseModel):
    contaminated_control_ids: list[str]
    treated_pre_ids: list[str]
    clean: bool


class DIDResponse(BaseModel):
    request_id: str
    service: str
    version: str
    status: str                       # "ok" | "refused"
    decomposition: Optional[Decomposition] = None
    clustered_se: Optional[ClusteredSE] = None
    reference_regression: Optional[ReferenceRegression] = None
    pretrend: Optional[PretrendDiagnostic] = None
    contamination: Optional[ContaminationReport] = None
    excluded_records: list[ExcludedRecord] = Field(default_factory=list)
    failures: list[Failure] = Field(default_factory=list)
    method_notes: list[str] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


class EventTimePoint(BaseModel):
    event_time: int
    n_treated_objects: int
    n_control_objects: int
    estimate: Optional[float] = None
    standard_error: Optional[float] = None
    supported: bool
    note: str = ""


class EventStudyResponse(BaseModel):
    request_id: str
    service: str
    version: str
    status: str
    points: list[EventTimePoint] = Field(default_factory=list)
    excluded_records: list[ExcludedRecord] = Field(default_factory=list)
    failures: list[Failure] = Field(default_factory=list)
    method_notes: list[str] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    db_path: str
