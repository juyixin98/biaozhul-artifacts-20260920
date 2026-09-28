"""Pydantic domain models for problems, plans and replay evidence.

Semantics summary (see README for the full contract):

* time lives on a non-negative integer grid, horizon ``H`` inclusive for
  starts and exclusive for occupations -- every action finishes by ``H``;
* every resource occupation is a half-open interval ``[start, end)``;
* at one timestamp the event order is fixed and documented:
  1. END  -- end effects of positive-duration actions finishing at t,
  2. PRE  -- precondition checks of every action starting at t,
  3. ZERO -- zero-duration actions starting at t apply their effects,
  4. INV  -- invariant checks of all actions active on segment [t,t+1),
  5. positive-duration starts occupy resources from t onward;
  write/write clashes between distinct actions at the same instant are
  rejected with SIMULTANEOUS_CONFLICT rather than silently ordered;
* duration invariants are checked at **every** grid point
  ``start .. end-1`` against the segment state (zero-duration: vacuous);
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .conditions import validate_condition, validate_effects

SUPPORTED_SIMULTANEITY_POLICY = "ENDS_FIRST_REJECT_WRITE_WRITES"


class Action(BaseModel):
    """A durative action from the finite known action set."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    duration_min: int = Field(ge=0)
    duration_max: int | None = Field(default=None, ge=0)
    precondition: dict[str, Any] | None = None
    invariant: dict[str, Any] | None = None
    effects: list[dict[str, Any]] | None = None
    resources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> "Action":
        if self.duration_max is None:
            object.__setattr__(self, "duration_max", self.duration_min)
        if self.duration_max < self.duration_min:
            raise ValueError(f"action {self.name}: duration_max < duration_min")
        validate_condition(self.precondition, path=f"action[{self.name}].precondition")
        validate_condition(self.invariant, path=f"action[{self.name}].invariant")
        validate_effects(self.effects)
        if any(not isinstance(r, str) or not r for r in self.resources):
            raise ValueError(f"action {self.name}: resource names must be non-empty strings")
        if len(set(self.resources)) != len(self.resources):
            raise ValueError(f"action {self.name}: duplicate resources in declaration")
        return self

    @property
    def duration_choices(self) -> range:
        assert self.duration_max is not None
        return range(self.duration_min, self.duration_max + 1)


class Problem(BaseModel):
    """A complete finite-horizon planning problem."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    horizon: int = Field(ge=1)
    initial: dict[str, Any] = Field(default_factory=dict)
    goal: dict[str, Any]
    resources: list[str] = Field(default_factory=list)
    actions: list[Action]
    simultaneity_policy: str = SUPPORTED_SIMULTANEITY_POLICY

    @field_validator("initial")
    @classmethod
    def _initial_values(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key, raw in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError("initial fact names must be non-empty strings")
            if not isinstance(raw, (int, bool)) or isinstance(raw, float):
                raise ValueError(f"initial.{key} must be int or bool, got {raw!r}")
        return value

    @field_validator("resources")
    @classmethod
    def _unique_resources(cls, value: list[str]) -> list[str]:
        if any(not isinstance(r, str) or not r for r in value):
            raise ValueError("resource names must be non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("duplicate resource declaration")
        return value

    @model_validator(mode="after")
    def _validate(self) -> "Problem":
        validate_condition(self.goal, path="goal")
        names = [a.name for a in self.actions]
        if len(set(names)) != len(names):
            raise ValueError("duplicate action names")
        if not self.actions:
            raise ValueError("at least one action is required")
        known = set(self.resources)
        for action in self.actions:
            unknown = set(action.resources) - known
            if unknown:
                raise ValueError(f"action {action.name}: unknown resources {sorted(unknown)}")
            if action.duration_max > self.horizon:
                raise ValueError(f"action {action.name}: duration_max exceeds horizon")
        if self.simultaneity_policy != SUPPORTED_SIMULTANEITY_POLICY:
            raise ValueError(
                f"unsupported simultaneity policy {self.simultaneity_policy!r}; "
                f"only {SUPPORTED_SIMULTANEITY_POLICY!r}"
            )
        return self

    def action(self, name: str) -> Action:
        for candidate in self.actions:
            if candidate.name == name:
                return candidate
        raise KeyError(name)


class ScheduledAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1)
    start: int = Field(ge=0)
    duration: int = Field(ge=0)

    @property
    def end(self) -> int:
        return self.start + self.duration


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[ScheduledAction]


class EventKind(str, Enum):
    INITIAL = "INITIAL"
    END = "END"
    START = "START"
    INVARIANT_CHECK = "INVARIANT_CHECK"
    ZERO_DURATION = "ZERO_DURATION"
    HORIZON = "HORIZON"


class TimelineEvent(BaseModel):
    """One evidence entry on the independently replayable timeline."""

    model_config = ConfigDict(extra="forbid")

    time: int
    order: int = Field(description="tie-break order among events at the same time")
    kind: EventKind
    action: str | None = None
    detail: str | None = None
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    active_actions: list[str]


class CheckOutcome(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"


class Violation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    time: int
    action: str | None = None
    resource: str | None = None
    message: str
    detail: str | None = None


class ReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: CheckOutcome
    goal_satisfied: bool
    events: list[TimelineEvent]
    violations: list[Violation]
    final_state: dict[str, Any]
    horizon: int

    @property
    def is_valid(self) -> bool:
        return self.outcome == CheckOutcome.VALID


class SearchStatus(str, Enum):
    OPTIMAL = "OPTIMAL"
    """A feasible plan was found and exhaustively proven optimal."""

    FEASIBLE_UNPROVEN = "FEASIBLE_UNPROVEN"
    """A feasible plan exists, but the search budget expired before proving optimality."""

    INFEASIBLE = "INFEASIBLE"
    """Exhaustive search (within the grid/horizon) proved no plan exists."""

    NO_PLAN_WITHIN_BUDGET = "NO_PLAN_WITHIN_BUDGET"
    """Budget expired before any feasible plan was found; feasibility unknown."""


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SearchStatus
    plan: Plan | None = None
    makespan: int | None = None
    nodes_expanded: int
    budget_nodes: int
    elapsed_ms: float
    optimal: bool
    reason: str
    explored_depth: int
