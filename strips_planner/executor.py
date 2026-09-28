"""Independent plan executor / verifier.

The executor shares *no code* with the search kernel beyond the immutable
model types and the transition rule in ``semantics``. It replays a proposed
action sequence step by step from the initial state and records the state
after every step. Any mismatch (unknown action, wrong arity, failed
precondition, goal not reached) is reported with the exact step number,
so a plan returned by the service can be proven valid independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import (
    ACTION_ARITY_MISMATCH,
    PRECONDITION_FAILED,
    UNKNOWN_ACTION,
    StateConflictError,
    ValidationError,
)
from .grounding import GroundProblem
from .model import State, atom_text
from .semantics import apply, goal_satisfied


@dataclass(frozen=True)
class StepRecord:
    index: int
    action: str
    cost: int
    state_before: State
    state_after: State


@dataclass(frozen=True)
class ExecutionReport:
    valid: bool
    goal_reached: bool
    total_cost: int
    steps: tuple[StepRecord, ...] = field(default_factory=tuple)
    failure: dict | None = None

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "goal_reached": self.goal_reached,
            "total_cost": self.total_cost,
            "steps": [
                {
                    "index": s.index,
                    "action": s.action,
                    "cost": s.cost,
                    "state_before": [atom_text(a) for a in sorted(s.state_before)],
                    "state_after": [atom_text(a) for a in sorted(s.state_after)],
                }
                for s in self.steps
            ],
            "failure": self.failure,
        }


def execute_plan(ground_problem: GroundProblem, plan: list[dict]) -> ExecutionReport:
    """Replay ``plan`` (a list of ``{"name", "args"}`` entries) independently.

    The returned report always contains the prefix executed up to (and
    including) the failing step's predecessor state.
    """
    index = {(a.schema_name, a.args): a for a in ground_problem.actions}
    state: State = ground_problem.initial
    records: list[StepRecord] = []
    total_cost = 0

    for step_no, raw in enumerate(plan):
        action = _resolve(raw, index, step_no)
        if isinstance(action, ExecutionReport):  # resolution failure
            return _with_prefix(action, records, total_cost)

        try:
            next_state = apply(state, action)
        except StateConflictError as exc:
            failure = {
                "category": StateConflictError.category,
                "code": PRECONDITION_FAILED,
                "step": step_no,
                "action": action.signature,
                "details": exc.details,
            }
            return _with_prefix(
                ExecutionReport(
                    valid=False, goal_reached=False, total_cost=total_cost,
                    failure=failure,
                ),
                records,
                total_cost,
            )

        records.append(
            StepRecord(
                index=step_no,
                action=action.signature,
                cost=action.cost,
                state_before=state,
                state_after=next_state,
            )
        )
        total_cost += action.cost
        state = next_state

    reached = goal_satisfied(
        state, ground_problem.goal_pos, ground_problem.goal_neg
    )
    if not reached:
        missing, forbidden = _goal_gap(ground_problem, state)
        return ExecutionReport(
            valid=False,
            goal_reached=False,
            total_cost=total_cost,
            steps=tuple(records),
            failure={
                "category": "state_conflict",
                "code": "GOAL_NOT_REACHED",
                "step": len(plan),
                "missing_positive": missing,
                "present_negative": forbidden,
            },
        )

    return ExecutionReport(
        valid=True, goal_reached=True, total_cost=total_cost,
        steps=tuple(records),
    )


def _resolve(raw, index, step_no):
    if not isinstance(raw, dict) or "name" not in raw:
        return ExecutionReport(
            valid=False, goal_reached=False, total_cost=0,
            failure={
                "category": "input_error",
                "code": UNKNOWN_ACTION,
                "step": step_no,
                "message": f"plan step {step_no} must be an object with 'name'",
            },
        )
    name = raw["name"]
    args = tuple(raw.get("args", []))
    if not isinstance(name, str) or not all(isinstance(a, str) for a in args):
        return ExecutionReport(
            valid=False, goal_reached=False, total_cost=0,
            failure={
                "category": "input_error",
                "code": ACTION_ARITY_MISMATCH,
                "step": step_no,
                "message": f"plan step {step_no} has non-string name/args",
            },
        )

    exact = index.get((name, args))
    if exact is not None:
        return exact

    same_name = [a for (n, _), a in index.items() if n == name]
    if not same_name:
        return ExecutionReport(
            valid=False, goal_reached=False, total_cost=0,
            failure={
                "category": "input_error",
                "code": UNKNOWN_ACTION,
                "step": step_no,
                "message": f"no action named {name!r}",
            },
        )
    return ExecutionReport(
        valid=False, goal_reached=False, total_cost=0,
        failure={
            "category": "input_error",
            "code": ACTION_ARITY_MISMATCH,
            "step": step_no,
            "message": (
                f"action {name!r} expects "
                f"{len(same_name[0].args)} arguments, got {len(args)}"
            ),
        },
    )


def _with_prefix(report: ExecutionReport, records, total_cost) -> ExecutionReport:
    return ExecutionReport(
        valid=False,
        goal_reached=False,
        total_cost=total_cost,
        steps=tuple(records),
        failure=report.failure,
    )


def _goal_gap(ground_problem: GroundProblem, state: State):
    missing = [atom_text(a) for a in sorted(ground_problem.goal_pos - state)]
    forbidden = [atom_text(a) for a in sorted(ground_problem.goal_neg & state)]
    return missing, forbidden
