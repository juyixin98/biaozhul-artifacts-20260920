"""Explicit propagation budgets.

Termination of label propagation is guaranteed structurally (labels are
antichains over a finite assumption set, so each (node, environment) pair
is processed at most once), but the number of environments can grow
exponentially.  These budgets make the termination bound explicit and turn
"ran out of budget" into a first-class, reportable outcome instead of a
silently wrong answer: the engine keeps the environments computed so far
and flags every subsequent query as incomplete.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Budgets:
    """Hard limits for one propagation run."""

    max_label_envs: int = 64  # environments kept in a single node label
    max_total_envs: int = 4096  # environment insertions across one run
    max_steps: int = 20000  # (node, environment) pairs dequeued per run


class BudgetExceeded(Exception):
    """Raised inside propagation when a budget is exhausted."""

    def __init__(self, kind: str, limit: int, context: str = "") -> None:
        self.kind = kind
        self.limit = limit
        self.context = context
        super().__init__(f"budget exceeded: {kind} limit {limit} ({context})")


class BudgetTracker:
    """Mutable counter for one propagation run, checked at every unit of work."""

    def __init__(self, budgets: Budgets) -> None:
        self.budgets = budgets
        self.steps = 0
        self.total_envs = 0

    def count_step(self, where: str) -> None:
        self.steps += 1
        if self.steps > self.budgets.max_steps:
            raise BudgetExceeded("steps", self.budgets.max_steps, where)

    def reserve_label_slot(self, node_id: str, current_label_size: int) -> None:
        """Account for one genuinely-new environment about to enter a label.

        Checked *before* mutation so a partial label never exceeds the cap.
        """
        self.total_envs += 1
        if current_label_size + 1 > self.budgets.max_label_envs:
            raise BudgetExceeded(
                "label_envs", self.budgets.max_label_envs, node_id
            )
        if self.total_envs > self.budgets.max_total_envs:
            raise BudgetExceeded(
                "total_envs", self.budgets.max_total_envs, node_id
            )
