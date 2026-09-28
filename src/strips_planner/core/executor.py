"""Independent plan executor.

The executor is the authority on whether a plan returned by search is really
executable. It deliberately does **not** consume any planner state: given only
the validated problem and a list of action labels it starts from
``problem.init`` itself and, one step at a time,

1. resolves the label to a grounded action (else ``UNKNOWN_ACTION``),
2. checks every positive and negative precondition against the *current*
   state (else ``MISSING_PRECONDITION`` / ``NEGATIVE_PRECONDITION_VIOLATED``),
3. applies the STRIPS transition and records full evidence,
4. checks the goal after the final step (else ``GOAL_NOT_REACHED``).

A failed step stops replay; the recorded trace identifies the step index, the
current state and the exact literals that caused the conflict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strips_planner.core.state import (
    State,
    apply_action,
    applicable,
    goal_satisfied,
    initial_state,
    missing_goal_atoms,
)
from strips_planner.errors import IssueCode
from strips_planner.models import GroundAction, Problem

STEP_OK = "OK"
STEP_FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class StepTrace:
    index: int
    action: str
    status: str
    state_before: list[str]
    state_after: list[str] | None
    cost: float
    reason: str | None = None
    violated_literals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "status": self.status,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "cost": self.cost,
            "reason": self.reason,
            "violated_literals": list(self.violated_literals),
        }


@dataclass(frozen=True, slots=True)
class ExecutionTrace:
    valid: bool
    goal_reached: bool
    failure_code: str | None
    failure_step: int | None
    message: str | None
    total_cost: float
    final_state: list[str]
    steps: tuple[StepTrace, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "goal_reached": self.goal_reached,
            "failure_code": self.failure_code,
            "failure_step": self.failure_step,
            "message": self.message,
            "total_cost": self.total_cost,
            "final_state": self.final_state,
            "steps": [s.to_dict() for s in self.steps],
        }


class PlanExecutor:
    def __init__(self, problem: Problem) -> None:
        self._problem = problem
        self._by_label: dict[str, GroundAction] = {}
        for action in problem.ground_actions:
            # Grounding combinations are unique, so labels never collide;
            # guard anyway so a malformed problem cannot shadow an action.
            if action.label in self._by_label:
                raise ValueError(f"duplicate grounded action label: {action.label}")
            self._by_label[action.label] = action

    def execute(self, labels: list[str], *, enforce_goal: bool = True) -> ExecutionTrace:
        state: State = initial_state(self._problem)
        steps: list[StepTrace] = []
        total_cost = 0.0

        for i, label in enumerate(labels):
            action = self._by_label.get(label)
            before = sorted(str(a) for a in state)
            if action is None:
                steps.append(StepTrace(
                    index=i, action=label, status=STEP_FAILED,
                    state_before=before, state_after=None, cost=0.0,
                    reason=f"no grounded action named {label!r}",
                    violated_literals=(),
                ))
                return self._fail(steps, total_cost, state,
                                  IssueCode.UNKNOWN_ACTION, i,
                                  f"step {i}: unknown action {label!r}")

            if not applicable(action, state):
                missing = tuple(f"+{a}" for a in sorted(action.pre_pos - state, key=str))
                forbidden = tuple(f"-{a}" for a in sorted(action.pre_neg & state, key=str))
                if missing:
                    code = IssueCode.MISSING_PRECONDITION
                    reason = f"step {i}: action {label} requires absent literals {list(missing)}"
                else:
                    code = IssueCode.NEGATIVE_PRECONDITION_VIOLATED
                    reason = f"step {i}: action {label} forbids present literals {list(forbidden)}"
                steps.append(StepTrace(
                    index=i, action=label, status=STEP_FAILED,
                    state_before=before, state_after=None, cost=0.0,
                    reason=reason, violated_literals=missing + forbidden,
                ))
                return self._fail(steps, total_cost, state, code, i, reason)

            next_state = apply_action(action, state)
            total_cost += action.cost
            steps.append(StepTrace(
                index=i, action=label, status=STEP_OK,
                state_before=before,
                state_after=sorted(str(a) for a in next_state),
                cost=action.cost,
            ))
            state = next_state

        reached = goal_satisfied(self._problem, state)
        if enforce_goal and not reached:
            reason = (f"plan executed {len(labels)} step(s) but goal is not reached; "
                      f"unresolved literals: {missing_goal_atoms(self._problem, state)}")
            return self._fail(steps, total_cost, state,
                              IssueCode.GOAL_NOT_REACHED, None, reason)

        return ExecutionTrace(
            valid=True,
            goal_reached=reached,
            failure_code=None,
            failure_step=None,
            message=None,
            total_cost=total_cost,
            final_state=sorted(str(a) for a in state),
            steps=tuple(steps),
        )

    def _fail(
        self,
        steps: list[StepTrace],
        total_cost: float,
        state: State,
        code: str,
        step: int | None,
        message: str,
    ) -> ExecutionTrace:
        return ExecutionTrace(
            valid=False,
            goal_reached=False,
            failure_code=code,
            failure_step=step,
            message=message,
            total_cost=total_cost,
            final_state=sorted(str(a) for a in state),
            steps=tuple(steps),
        )
