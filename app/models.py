"""Request/response models and input validation for the RTA service.

Analysis model (the assumptions the analysis is conditional on):

* Single preemptive core.
* Independent tasks: no precedence constraints, no shared resources that
  would require explicit locking analysis.  ``blocking`` is supplied by the
  caller as a *conservative upper bound* on the time the task may be kept from
  running by lower-priority activity (e.g. priority-ceiling/ceiling-priority
  blocking).  The service does not derive or prove it.
* Tasks are periodic, released simultaneously at t = 0 (critical instant).
* Fixed priorities.  A *smaller* ``priority`` integer denotes a *higher*
  priority (numerical priority order).  Equal priorities are rejected:
  without a tie-break rule (FIFO within a level, round-robin, ...) the RTA
  recurrence used here is undefined, so the model forbids ties outright.
* Constrained deadlines: D_i <= T_i.
* All values are non-negative integers (discrete time ticks).
"""

from __future__ import annotations

import re
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Priority integers must stay in a sane, unambiguous range.
MIN_PRIORITY = 1
MAX_PRIORITY = 1_000_000
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class Task(BaseModel):
    """One periodic task."""

    id: str = Field(..., description="Unique non-empty task identifier.")
    wcet: int = Field(
        ...,
        ge=0,
        description="Worst-case execution time upper bound C_i (integer ticks, >= 0).",
    )
    period: int = Field(
        ...,
        ge=1,
        description="Period / minimum inter-arrival T_i (integer ticks, >= 1).",
    )
    deadline: int = Field(
        ...,
        ge=0,
        description="Relative deadline D_i (integer ticks, must satisfy D_i <= T_i).",
    )
    blocking: int = Field(
        default=0,
        ge=0,
        description=(
            "Blocking upper bound B_i from lower-priority activity (integer ticks, >= 0). "
            "Caller-supplied conservative bound; the service does not derive it."
        ),
    )
    priority: int = Field(
        ...,
        ge=MIN_PRIORITY,
        le=MAX_PRIORITY,
        description=(
            "Fixed priority: smaller integer == higher priority. "
            "Must be unique across the task set (equal priorities are rejected)."
        ),
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("task id must be a non-empty string")
        if not _TASK_ID_RE.match(v):
            raise ValueError(
                "task id may contain only letters, digits, '.', '_' and '-'"
            )
        return v


class AnalysisOptions(BaseModel):
    """Optional toggles for the analysis."""

    run_simulation: bool = Field(
        default=True,
        description="Also run the discrete-event scheduling reference and cross-check RTA.",
    )
    max_iterations: int = Field(
        default=10_000,
        ge=1,
        le=1_000_000,
        description="Hard guard on RTA iterations (convergence failure otherwise).",
    )
    max_response_ticks: int = Field(
        default=10**12,
        ge=1,
        description="Hard guard on response-time magnitude (overflow otherwise).",
    )


class AnalysisRequest(BaseModel):
    """A task set to analyze."""

    tasks: List[Task] = Field(..., min_length=1)
    options: AnalysisOptions = Field(default_factory=AnalysisOptions)

    @model_validator(mode="after")
    def _validate_taskset(self) -> "AnalysisRequest":
        ids = [t.id for t in self.tasks]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate task ids: {', '.join(dupes)}")

        priorities = [t.priority for t in self.tasks]
        if len(set(priorities)) != len(priorities):
            dupes = sorted({p for p in priorities if priorities.count(p) > 1})
            raise ValueError(
                "equal priorities are not permitted under the fixed-priority model; "
                f"shared priority value(s): {dupes}. Assign distinct priorities."
            )

        for t in self.tasks:
            if t.deadline > t.period:
                raise ValueError(
                    f"task {t.id}: deadline D={t.deadline} exceeds period T={t.period}; "
                    "the analysis model requires constrained deadlines (D <= T)"
                )
        return self


class IterationStep(BaseModel):
    """One iteration of the response-time recurrence for a task."""

    n: int = Field(..., description="Iteration index; n=0 is the initial value R_0.")
    r: int = Field(..., description="Response-time candidate R_n at this iteration.")
    interference: dict = Field(
        default_factory=dict,
        description="Interference contribution per higher-priority task at R_n, in ticks.",
    )
    total_interference: int = Field(
        ..., description="Sum of higher-priority interference at R_n."
    )
    next_r: Optional[int] = Field(
        default=None,
        description="Next candidate R_(n+1) produced by the recurrence (absent on the last step).",
    )


class TaskResult(BaseModel):
    """Per-task analysis outcome."""

    id: str
    priority: int
    wcet: int
    period: int
    deadline: int
    blocking: int
    schedulable: bool
    response_time: Optional[int] = Field(
        default=None,
        description=(
            "Fixed-point worst-case response time WCRT (ticks) when reached; "
            "None if iteration overflowed or failed to converge."
        ),
    )
    slack: Optional[int] = Field(default=None, description="D_i - WCRT when converged.")
    interference_sources: List[dict] = Field(
        default_factory=list,
        description="Each higher-priority task that interferes, with period, wcet and count form.",
    )
    iterations: List[IterationStep]
    terminal_condition: str = Field(
        ...,
        description=(
            "fixed_point | deadline_exceeded | iteration_limit | value_overflow | empty_taskset_internal"
        ),
    )
    failed_deadline: Optional[int] = Field(
        default=None,
        description="The relative deadline that was missed (None when the task met its deadline)."
        ,
    )
    failure_detail: Optional[str] = None
    # Continued (diagnostic) fixed point after a deadline miss, if obtained.
    continued_fixed_point: Optional[int] = None
    rta_crosscheck: Optional[str] = Field(
        default=None,
        description="RTA vs discrete simulation agreement for the critical instant: agree | disagree | inconclusive | skipped.",
    )


class SimulationResult(BaseModel):
    """Discrete-event fixed-priority scheduling reference."""

    enabled: bool
    horizon_ticks: int
    horizon_basis: str = Field(
        ..., description="hyperperiod | capped | released_jobs_cap"
    )
    truncated: bool
    released_jobs: int
    completed_jobs: int
    deadline_misses: int
    first_miss: Optional[dict] = None
    first_job_response_times: dict = Field(
        default_factory=dict,
        description="Response time of the t=0 job of every task (critical instant), ticks."
    )
    baseline_zero_blocking: Optional[dict] = Field(
        default=None,
        description="Reference simulation with all blocking set to zero (pure fixed-priority preemptive).",
    )
    crosscheck: str = Field(
        ..., description="agree | disagree | inconclusive"
    )
    crosscheck_detail: Optional[str] = None


class AnalysisResponse(BaseModel):
    schedulable: bool
    model_assumptions: List[str]
    tasks: List[TaskResult]
    simulation: SimulationResult
    utilization: dict = Field(
        ...,
        description=(
            "Informational processor utilization only. NOT a sufficient condition: "
            "low/average utilization never implies schedulability under fixed priorities."
        ),
    )
    integrity: dict = Field(
        ...,
        description="Real cryptographic checksum (SHA-256) of the canonical request payload."
    )


class ErrorResponse(BaseModel):
    error: str
    code: str
    detail: str
