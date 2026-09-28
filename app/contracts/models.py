"""Statistical contracts.

This package owns the *boundary vocabulary* of the service: the shape of panel
data, every explicit failure category the engine may raise, and the response
envelopes that travel back over HTTP. The estimation core and diagnostics
depend on these types; the types never depend on the engine (dependency rule),
so a consumer can program against this module without importing NumPy.

Design notes
------------
* A missing period for a unit is a distinct state, never an implicit zero row.
  The contract models observations sparsely; alignment is the core's job.
* Treatment is an explicit per-(unit, period) state, not inferred from the
  outcome, so contamination (a control that is ever treated) is detectable.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# Failure vocabulary
# --------------------------------------------------------------------------- #
class FailureCategory(str, Enum):
    """Exhaustive, machine-readable reasons the engine may reject a request."""

    EMPTY_PANEL = "EMPTY_PANEL"
    DUPLICATE_UNIT_PERIOD = "DUPLICATE_UNIT_PERIOD"
    NON_FINITE_OUTCOME = "NON_FINITE_OUTCOME"
    INVALID_WEIGHT = "INVALID_WEIGHT"
    NON_BINARY_TREATMENT = "NON_BINARY_TREATMENT"
    SINGLE_PERIOD = "SINGLE_PERIOD"
    SINGLE_GROUP = "SINGLE_GROUP"
    NOT_TWO_PERIODS = "NOT_TWO_PERIODS"
    # Balance strategy "balanced" was requested but a unit lacks a period.
    UNBALANCED_PANEL = "UNBALANCED_PANEL"
    # Unit has observations in both periods but records different group/treatment
    # path than its observed treatment path supports (cross-state inconsistency).
    INCONSISTENT_TREATMENT_PATH = "INCONSISTENT_TREATMENT_PATH"
    # A unit designated/usable as control is treated in a period (contamination).
    CONTROL_GROUP_CONTAMINATED = "CONTROL_GROUP_CONTAMINATED"
    # Not-yet- or already-treated comparison requested outside available support.
    OUT_OF_SUPPORT_EVENT_TIME = "OUT_OF_SUPPORT_EVENT_TIME"
    # Event-time window cannot be estimated: never-treated controls absent.
    NO_VALID_CONTROL_GROUP = "NO_VALID_CONTROL_GROUP"
    # Design matrix rank deficiency / no variation in the regressor of interest.
    SINGULAR_DESIGN = "SINGULAR_DESIGN"
    # n - rank <= 0: the point estimate may be identified but there are no
    # residual degrees of freedom, so a (cluster-robust) variance is undefined.
    NO_RESIDUAL_DEGREES = "NO_RESIDUAL_DEGREES"
    # Too few clusters for cluster-robust inference to be meaningful.
    INSUFFICIENT_CLUSTERS = "INSUFFICIENT_CLUSTERS"
    # No units survive the requested alignment/balance/weight policy.
    NO_ESTIMABLE_UNITS = "NO_ESTIMABLE_UNITS"
    INVALID_REQUEST = "INVALID_REQUEST"


class DiagnosticLevel(str, Enum):
    OK = "OK"
    WARNING = "WARNING"
    INDETERMINATE = "INDETERMINATE"
    FAILED = "FAILED"


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
class Observation(BaseModel):
    """One (unit, period) cell. A unit absent for a period is simply absent."""

    unit_id: str = Field(..., min_length=1)
    period: int
    y: float
    # Whether the unit is *currently* treated in this period. Explicit state.
    treated: bool = False
    # Observation weight. Fixed across the run; see WeightPolicy.
    weight: float = Field(default=1.0)

    @field_validator("y")
    @classmethod
    def _y_finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("y must be finite")
        return v

    @field_validator("weight")
    @classmethod
    def _w_nonnegative(cls, v: float) -> float:
        if v != v or v < 0:
            raise ValueError("weight must be finite and non-negative")
        return v


class BalanceStrategy(str, Enum):
    # Keep only units observed in every selected period. This is required for a
    # valid within-unit first difference and is the only strategy offered: a
    # unit missing a period is excluded-with-record rather than used in a
    # period-by-period cross-section (where identity alignment is impossible).
    BALANCED = "balanced"


class WeightPolicy(str, Enum):
    # One vote per unit; weights fixed at the unit's baseline weight and reused
    # for every period so first differences are not re-weighted mid-run.
    UNIT_FIXED = "unit_fixed"
    OBSERVATION = "observation"
    NONE = "none"


class ControlGroup(str, Enum):
    NEVER_TREATED = "never_treated"
    NOT_YET_TREATED = "not_yet_treated"


class DIDRequest(BaseModel):
    request_id: str = Field(..., min_length=1)
    observations: list[Observation] = Field(default_factory=list)
    # Which two periods to difference, ascending. None -> the two observed.
    pre_period: int | None = None
    post_period: int | None = None
    balance: BalanceStrategy = BalanceStrategy.BALANCED
    weight_policy: WeightPolicy = WeightPolicy.UNIT_FIXED
    control_group: ControlGroup = ControlGroup.NEVER_TREATED
    # When true, contaminated controls raise CONTROL_GROUP_CONTAMINATED instead
    # of merely being excluded-with-a-record.
    reject_on_contamination: bool = True
    # Significance level for CI / diagnostics.
    alpha: float = Field(default=0.05, gt=0.0, lt=1.0)


class EventStudyRequest(DIDRequest):
    # Event-time window relative to treatment onset, e.g. -2..2. Cohorts whose
    # support cannot cover an offset are excluded for that offset, and if the
    # requested offset has no support the model explicitly refuses.
    min_event_time: int | None = None
    max_event_time: int | None = None
    # Require never-treated (True) vs allow not-yet-treated comparison.
    strict_support: bool = True


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
class ExcludedRecord(BaseModel):
    """A single row explaining why data did not enter the estimator."""

    unit_id: str
    reason: FailureCategory
    detail: str
    periods: list[int] = Field(default_factory=list)


class CellMeans(BaseModel):
    """The four hand-checkable means of the 2x2 design."""

    treat_pre: float
    treat_post: float
    control_pre: float
    control_post: float
    n_treat: int
    n_control: int


class Decomposition(BaseModel):
    """Difference-in-differences decomposition, each piece independently."""

    cells: CellMeans
    treat_change: float          # (treat_post - treat_pre)
    control_change: float        # (control_post - control_pre)
    did: float                   # treat_change - control_change


class Estimate(BaseModel):
    name: str
    value: float
    se: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    t_stat: float | None = None
    p_value: float | None = None
    dof: int | None = None
    n_units: int
    n_obs: int
    n_clusters: int


class Diagnostic(BaseModel):
    name: str
    level: DiagnosticLevel
    message: str
    statistic: float | None = None
    p_value: float | None = None
    # Parallel trends is diagnosable/violatable but never provable.
    caveat: str | None = None


class StepTrace(BaseModel):
    """One auditable processing step, keyed to request identity."""

    step: str
    detail: str
    n_units_in: int
    n_units_out: int


class DIDResult(BaseModel):
    request_id: str
    core_version: str
    status: str  # "ok" | "rejected"
    estimate: Estimate | None = None
    decomposition: Decomposition | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    excluded: list[ExcludedRecord] = Field(default_factory=list)
    steps: list[StepTrace] = Field(default_factory=list)
    weight_policy: WeightPolicy
    balance: BalanceStrategy
    cluster_variable: str = "unit_id"


class EventTimePoint(BaseModel):
    event_time: int
    estimate: float
    se: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    n_cohorts: int
    n_units: int
    supported: bool


class EventStudyResult(BaseModel):
    request_id: str
    core_version: str
    status: str
    points: list[EventTimePoint] = Field(default_factory=list)
    reference_period: int = -1
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    excluded: list[ExcludedRecord] = Field(default_factory=list)
    steps: list[StepTrace] = Field(default_factory=list)


class FailureEnvelope(BaseModel):
    request_id: str
    core_version: str
    status: str = "rejected"
    failure_category: FailureCategory
    message: str
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    excluded: list[ExcludedRecord] = Field(default_factory=list)
    steps: list[StepTrace] = Field(default_factory=list)
