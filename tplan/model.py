"""Domain model for temporal planning with a finite, discrete action set.

Semantics summary (see docs/SEMANTICS.md for the full contract):

* Time is a finite integer grid ``0 .. horizon``. All action durations and
  event times are integers on this grid.
* State is an assignment of *fluents* to exact rational values
  (``fractions.Fraction``), so arithmetic never drifts.
* Every action has a *start condition* (checked at the start instant), a
  *durational invariant* (checked at every grid point the action is alive,
  including interior points -- not only at the two ends), and *effects*
  applied atomically at the end instant.
* Renewable resource usage occupies the half-open interval ``[start, end)``;
  the resource is free again exactly at ``end``. Consumable resources are
  debited at start and credited at end.
* Events happening at the same time are resolved with a deterministic total
  order (``END`` before ``START``, then ``action_id``); schedules that would
  make the result ambiguous are rejected with ``SIMULTANEOUS_CONFLICT``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction

from .conditions import Condition, condition_from_dict

__all__ = [
    "ResourceKind",
    "Resource",
    "EffectOp",
    "EffectSpec",
    "ActionSpec",
    "Problem",
    "ScheduledAction",
    "EventKind",
    "TimelineEvent",
    "FailureCode",
]


class ResourceKind(str, Enum):
    RENEWABLE = "renewable"
    CONSUMABLE = "consumable"


class EffectOp(str, Enum):
    ADD = "+"
    SUB = "-"
    ASSIGN = "="
    MUL = "*"


class EventKind(str, Enum):
    INIT = "init"
    START = "start"
    END = "end"
    TICK = "tick"  # zero-duration action: start and end collapse to one instant


class FailureCode(str, Enum):
    """Explicit failure categories -- callers never get a bare success."""

    INVALID_PROBLEM = "invalid_problem"
    START_CONDITION_VIOLATED = "start_condition_violated"
    INVARIANT_VIOLATED = "invariant_violated"
    RESOURCE_CONFLICT = "resource_conflict"
    SIMULTANEOUS_CONFLICT = "simultaneous_conflict"
    UNSAT_PROVEN = "unsat_proven"
    BUDGET_EXHAUSTED = "budget_exhausted"
    REPLAY_MISMATCH = "replay_mismatch"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class Resource:
    id: str
    capacity: int
    kind: ResourceKind = ResourceKind.RENEWABLE


@dataclass(frozen=True)
class EffectSpec:
    fluent: str
    op: EffectOp
    amount: Fraction

    def apply(self, value: Fraction) -> Fraction:
        if self.op is EffectOp.ADD:
            return value + self.amount
        if self.op is EffectOp.SUB:
            return value - self.amount
        if self.op is EffectOp.ASSIGN:
            return self.amount
        return value * self.amount


@dataclass(frozen=True)
class ActionSpec:
    id: str
    duration: int
    start_condition: tuple[Condition, ...]
    invariant: tuple[Condition, ...]
    effects: tuple[EffectSpec, ...]
    resource_use: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ScheduledAction:
    action_id: str
    start: int
    end: int


@dataclass(frozen=True)
class TimelineEvent:
    """One entry of an independently replayable timeline."""

    time: int
    order: int
    kind: EventKind
    action_id: str
    state: dict[str, str]
    resources: dict[str, int]
    note: str


@dataclass(frozen=True)
class Problem:
    horizon: int
    fluents: dict[str, Fraction]
    resources: dict[str, Resource]
    actions: dict[str, ActionSpec]
    goal: Condition
    action_order: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict) -> "Problem":
        """Parse and validate a JSON-serialisable problem description.

        Raises ``ValueError`` (mapped to ``INVALID_PROBLEM`` upstream).
        """
        if not isinstance(data, dict):
            raise ValueError("problem body must be an object")

        horizon = data.get("horizon")
        if not isinstance(horizon, int) or horizon < 0:
            raise ValueError("horizon must be a non-negative integer")

        raw_fluents = data.get("fluents", {})
        if not isinstance(raw_fluents, dict):
            raise ValueError("fluents must be an object of id -> initial value")
        fluents: dict[str, Fraction] = {}
        for fid, val in raw_fluents.items():
            if not isinstance(fid, str) or not fid:
                raise ValueError("fluent ids must be non-empty strings")
            try:
                fluents[fid] = Fraction(val)
            except (TypeError, ValueError, ZeroDivisionError) as exc:
                raise ValueError(f"fluent {fid!r} has invalid initial value: {val!r}") from exc

        resources: dict[str, Resource] = {}
        for rd in data.get("resources", []):
            rid = rd.get("id")
            if not isinstance(rid, str) or not rid:
                raise ValueError("resource id must be a non-empty string")
            if rid in resources:
                raise ValueError(f"duplicate resource id {rid!r}")
            cap = rd.get("capacity")
            if not isinstance(cap, int) or cap < 1:
                raise ValueError(f"resource {rid!r} capacity must be an integer >= 1")
            kind = ResourceKind(rd.get("kind", "renewable"))
            resources[rid] = Resource(id=rid, capacity=cap, kind=kind)

        raw_actions = data.get("actions", [])
        if not isinstance(raw_actions, list) or not raw_actions:
            raise ValueError("actions must be a non-empty list")
        actions: dict[str, ActionSpec] = {}
        action_order: list[str] = []
        for ad in raw_actions:
            aid = ad.get("id")
            if not isinstance(aid, str) or not aid:
                raise ValueError("action id must be a non-empty string")
            if aid in actions:
                raise ValueError(f"duplicate action id {aid!r}")
            duration = ad.get("duration")
            if not isinstance(duration, int) or duration < 0:
                raise ValueError(f"action {aid!r} duration must be a non-negative integer")

            def parse_conds(key: str) -> tuple[Condition, ...]:
                conds = []
                for i, cd in enumerate(ad.get(key, [])):
                    try:
                        conds.append(condition_from_dict(cd, known_fluents=set(fluents)))
                    except ValueError as exc:
                        raise ValueError(f"action {aid!r} {key}[{i}]: {exc}") from exc
                return tuple(conds)

            start_cond = parse_conds("start_condition")
            invariant = parse_conds("invariant")

            effects: list[EffectSpec] = []
            for ed in ad.get("effects", []):
                fid = ed.get("fluent")
                if fid not in fluents:
                    raise ValueError(f"action {aid!r} effect on undeclared fluent {fid!r}")
                try:
                    op = EffectOp(ed.get("op", "+"))
                except ValueError as exc:
                    raise ValueError(f"action {aid!r}: unknown effect op {ed.get('op')!r}") from exc
                try:
                    amount = Fraction(ed.get("amount", 0))
                except (TypeError, ValueError, ZeroDivisionError) as exc:
                    raise ValueError(f"action {aid!r}: invalid effect amount") from exc
                effects.append(EffectSpec(fluent=fid, op=op, amount=amount))

            use: dict[str, int] = {}
            for ur in ad.get("resource_use", []):
                rid = ur.get("resource")
                if rid not in resources:
                    raise ValueError(f"action {aid!r} uses undeclared resource {rid!r}")
                qty = ur.get("amount")
                if not isinstance(qty, int) or qty < 0:
                    raise ValueError(f"action {aid!r} resource {rid!r}: amount must be >= 0")
                if qty > resources[rid].capacity:
                    raise ValueError(
                        f"action {aid!r} requests {qty} of {rid!r} but capacity is "
                        f"{resources[rid].capacity}"
                    )
                use[rid] = qty
            actions[aid] = ActionSpec(
                id=aid,
                duration=duration,
                start_condition=tuple(start_cond),
                invariant=tuple(invariant),
                effects=tuple(effects),
                resource_use=use,
            )
            action_order.append(aid)

        try:
            goal = condition_from_dict(data.get("goal", {"all": []}), known_fluents=set(fluents))
        except ValueError as exc:
            raise ValueError(f"goal: {exc}") from exc

        return cls(
            horizon=horizon,
            fluents=fluents,
            resources=resources,
            actions=actions,
            goal=goal,
            action_order=tuple(action_order),
        )
