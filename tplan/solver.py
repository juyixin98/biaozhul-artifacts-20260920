"""Planning kernel: finite-action temporal planner on a small integer grid.

Search strategy
---------------
Depth-first forward search over decision points. A state records the fluent
values, per-resource levels, and the set of actions currently alive
``(action_id -> end_time)``. At each grid time ``t`` we:

1. **End** every action whose end is ``t``: apply its effects and release
   its resources. Ends at one instant are mutually exclusive (enforced both
   here and by the simulator).
2. **Choose a start subset** at ``t`` under the policy "at most one start per
   instant, and a start at ``t`` is allowed in the same instant as an end
   (end-before-start ordering)". Starting nothing is always an option (the
   search advances time).
3. At the new state, verify every alive action's invariant at ``t``.

Optimality
----------
Actions are unit-cost (plan quality = number of started actions; a secondary
criterion is earliest goal time). The search first finds *a* feasible plan,
then keeps proving optimality within the budget. If the budget expires with a
feasible plan in hand, the result is reported as ``feasible / optimality not
proven`` (``BUDGET_EXHAUSTED``) -- it is never silently called optimal.
Exhaustion of the search space without a plan is ``UNSAT_PROVEN``.

The solver never validates itself: every returned plan is replayed through
``simulator.simulate`` (the independent kernel) before it is accepted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Mapping

from .model import (
    ActionSpec,
    EventKind,
    FailureCode,
    Problem,
    ResourceKind,
    ScheduledAction,
)
from .simulator import SimulationError, simulate


@dataclass(frozen=True)
class Budget:
    node_limit: int = 100_000
    deadline_monotonic: float | None = None  # time.monotonic() deadline

    def expired(self, nodes_used: int, now: float) -> bool:
        if nodes_used >= self.node_limit:
            return True
        if self.deadline_monotonic is not None and now >= self.deadline_monotonic:
            return True
        return False


@dataclass
class SolveResult:
    status: str  # "optimal" | "feasible_not_proven_optimal" | "unsat" | "invalid"
    failure_code: FailureCode | None
    schedule: tuple[ScheduledAction, ...]
    nodes_expanded: int
    best_cost: int | None
    goal_time: int | None
    message: str
    trace: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def found(self) -> bool:
        return self.status in ("optimal", "feasible_not_proven_optimal")


@dataclass(frozen=True)
class _Alive:
    end: int


@dataclass
class _Node:
    t: int
    state: dict[str, Fraction]
    renewable: dict[str, int]
    consumable: dict[str, int]
    alive: dict[str, int]  # action_id -> end time (currently running)
    ends: dict[str, int]  # end time -> action_id, over whole prefix (conflict pruning)
    counts: dict[str, int]  # action_id -> number of starts on this prefix
    schedule: tuple[ScheduledAction, ...]


class Solver:
    def __init__(self, problem: Problem, budget: Budget | None = None, max_repeats: int = 64):
        self.p = problem
        self.budget = budget or Budget()
        self.max_repeats = max_repeats  # safety cap on starts of the same action
        self.nodes = 0
        self.best: tuple[ScheduledAction, ...] | None = None
        self.best_cost: int | None = None
        self.goal_time: int | None = None
        self.trace: list[dict] = []

    # -- public entry -------------------------------------------------------

    def solve(self) -> SolveResult:
        root = _Node(
            t=0,
            state=dict(self.p.fluents),
            renewable={
                rid: 0 for rid, r in self.p.resources.items() if r.kind is ResourceKind.RENEWABLE
            },
            consumable={
                rid: r.capacity
                for rid, r in self.p.resources.items()
                if r.kind is ResourceKind.CONSUMABLE
            },
            alive={},
            ends={},
            counts={},
            schedule=(),
        )
        budget_hit = False
        try:
            budget_hit = self._dfs(root, depth=0)
        except _BudgetStop:
            budget_hit = True

        if self.best is not None:
            # Independent replay validation before accepting ANY plan.
            verified = self._verify(self.best)
            if verified is not None:
                return verified  # SolveResult describing the replay failure
            if budget_hit:
                return SolveResult(
                    status="feasible_not_proven_optimal",
                    failure_code=FailureCode.BUDGET_EXHAUSTED,
                    schedule=self.best,
                    nodes_expanded=self.nodes,
                    best_cost=self.best_cost,
                    goal_time=self.goal_time,
                    message=(
                        f"feasible plan with {self.best_cost} action(s) reaching the goal at "
                        f"t={self.goal_time}; search budget exhausted before optimality was proven"
                    ),
                    trace=tuple(self.trace),
                )
            return SolveResult(
                status="optimal",
                failure_code=None,
                schedule=self.best,
                nodes_expanded=self.nodes,
                best_cost=self.best_cost,
                goal_time=self.goal_time,
                message=(
                    f"optimal plan: {self.best_cost} action(s), goal first reached at "
                    f"t={self.goal_time}; search space exhausted"
                ),
                trace=tuple(self.trace),
            )

        if budget_hit:
            return SolveResult(
                status="budget_exhausted",
                failure_code=FailureCode.BUDGET_EXHAUSTED,
                schedule=(),
                nodes_expanded=self.nodes,
                best_cost=None,
                goal_time=None,
                message="budget exhausted before any feasible plan was found",
                trace=tuple(self.trace),
            )
        return SolveResult(
            status="unsat",
            failure_code=FailureCode.UNSAT_PROVEN,
            schedule=(),
            nodes_expanded=self.nodes,
            best_cost=None,
            goal_time=None,
            message=f"no schedule reaches the goal within horizon {self.p.horizon} (search space exhausted)",
            trace=tuple(self.trace),
        )

    # -- search -------------------------------------------------------------

    def _tick_budget(self) -> None:
        self.nodes += 1
        if self.budget.expired(self.nodes, time.monotonic()):
            self.trace.append(
                {"event": "budget_exhausted", "nodes": self.nodes, "best_cost": self.best_cost}
            )
            raise _BudgetStop

    def _dfs(self, node: _Node, depth: int) -> bool:
        """Resolve events at ``node.t`` then recurse to ``t + 1``.

        On entry ``node`` carries the state *before* events at ``node.t`` and
        an ``alive`` map that may contain actions ending exactly at ``node.t``.
        Order at the instant: ends first, then at most one start, then the
        invariant check for every action alive over the instant.
        """
        self._tick_budget()

        if self.best_cost == 0:
            return False

        t = node.t

        # 1. Ends at t (end-before-start ordering).
        state = dict(node.state)
        renewable = dict(node.renewable)
        consumable = dict(node.consumable)
        alive: dict[str, int] = {}
        for aid, end in node.alive.items():
            if end == t:
                act = self.p.actions[aid]
                for eff in act.effects:
                    state[eff.fluent] = eff.apply(state[eff.fluent])
                for rid, qty in act.resource_use.items():
                    if rid in renewable:
                        renewable[rid] -= qty
                    else:
                        consumable[rid] += qty
            else:
                alive[aid] = end

        # 2. An end alone (no start follows) is the "idle at t" resolution.
        ended_node = _Node(
            t=t,
            state=state,
            renewable=renewable,
            consumable=consumable,
            alive=alive,
            ends=node.ends,
            counts=node.counts,
            schedule=node.schedule,
        )

        # 2b. Note: invariants of actions still alive at t are checked AFTER
        #     the start choice below, because a zero-duration action starting
        #     at t applies effects at that same instant and may legitimately
        #     repair the state (TICKs share an instant with an action's
        #     interior point, just not with another start/end event).

        if not alive and self.p.goal.evaluate(state):
            self._accept(ended_node)
            # A goal achieved after full completion cannot be improved here.
            return False

        if t > self.p.horizon:
            return False

        # 3. Choices at t: commit to an action first (in declaration order),
        #    idling last. Trying starts first reaches a feasible plan quickly
        #    (important when the budget is tight); exhaustive backtracking
        #    still proves optimality when the budget allows.
        budget_hit = False
        choices: list[ActionSpec | None] = list(self._startable(ended_node))
        choices.append(None)
        for act in choices:
            if act is None:
                child = ended_node
                if not self._alive_invariants_hold(alive, state, t):
                    # Idling through t violates an invariant; a TICK starting
                    # at this same instant may still repair it (other choices).
                    continue
            else:
                child = self._apply_start(ended_node, act)
                if child is None:
                    continue
            if self.best_cost is not None and len(child.schedule) > self.best_cost:
                continue
            # A zero-duration start can complete the plan at t; a
            # positive-duration start leaves an action alive, so the goal
            # cannot be accepted while work is still running.
            if not child.alive and self.p.goal.evaluate(child.state):
                self._accept(child)
                if self.best_cost == 0:
                    return False
                continue  # no need to extend this branch
            if t >= self.p.horizon:
                continue
            nxt = _Node(
                t=t + 1,
                state=child.state,
                renewable=child.renewable,
                consumable=child.consumable,
                alive=child.alive,
                ends=child.ends,
                counts=child.counts,
                schedule=child.schedule,
            )
            if self._dfs(nxt, depth + 1):
                budget_hit = True
                break
        return budget_hit

    def _alive_invariants_hold(self, alive: dict[str, int], state: Mapping, t: int) -> bool:
        """True if every action alive over instant t satisfies its invariant."""
        for aid in sorted(alive):
            for c in self.p.actions[aid].invariant:
                if not c.evaluate(state):
                    self.trace.append(
                        {"event": "prune_invariant", "t": t, "action": aid, "condition": c.describe()}
                    )
                    return False
        return True

    def _startable(self, node: _Node) -> list[ActionSpec]:
        """Actions whose start at ``node.t`` is structurally admissible.

        Semantic checks (conditions, capacity, instant collisions) happen in
        ``_apply_start``; this only fixes the candidate set.
        """
        t = node.t
        out: list[ActionSpec] = []
        for aid in self.p.action_order:
            act = self.p.actions[aid]
            if aid in node.alive:
                continue
            if t + act.duration > self.p.horizon:
                continue
            if node.counts.get(aid, 0) >= self.max_repeats:
                continue
            end_t = t if act.duration == 0 else t + act.duration
            # No two event instances may share an instant: an end/tick at
            # end_t would collide with this action's end (a TICK at t is
            # itself both start and end, so this also rejects a TICK sharing
            # an instant with another action's end).
            if end_t in node.ends:
                continue
            out.append(act)
        return out

    def _apply_start(self, node: _Node, act: ActionSpec) -> _Node | None:
        """Apply one start at ``node.t`` to an already end-resolved node."""
        t = node.t
        state = dict(node.state)
        renewable = dict(node.renewable)
        consumable = dict(node.consumable)
        alive = dict(node.alive)
        ends = dict(node.ends)
        counts = dict(node.counts)

        for c in act.start_condition:
            if not c.evaluate(state):
                self.trace.append(
                    {"event": "prune_start_condition", "t": t, "action": act.id, "condition": c.describe()}
                )
                return None
        for rid, qty in act.resource_use.items():
            if rid in renewable:
                if renewable[rid] + qty > self.p.resources[rid].capacity:
                    self.trace.append(
                        {"event": "prune_resource", "t": t, "action": act.id, "resource": rid}
                    )
                    return None
            else:
                if consumable[rid] - qty < 0:
                    self.trace.append(
                        {"event": "prune_resource", "t": t, "action": act.id, "resource": rid}
                    )
                    return None
        for rid, qty in act.resource_use.items():
            if rid in renewable:
                renewable[rid] += qty
            else:
                consumable[rid] -= qty

        item = ScheduledAction(act.id, t, t + act.duration)
        counts[act.id] = counts.get(act.id, 0) + 1
        if act.duration == 0:
            for eff in act.effects:
                state[eff.fluent] = eff.apply(state[eff.fluent])
            for rid, qty in act.resource_use.items():
                if rid in renewable:
                    renewable[rid] -= qty
                else:
                    consumable[rid] += qty
            for c in act.invariant:
                if not c.evaluate(state):
                    self.trace.append(
                        {"event": "prune_invariant", "t": t, "action": act.id, "condition": c.describe()}
                    )
                    return None
            ends[t] = act.id  # TICK occupies the instant as both start/end
        else:
            alive[act.id] = t + act.duration
            ends[t + act.duration] = act.id

        # Durational invariants at t over the half-open alive window.
        for aid in sorted(alive):
            for c in self.p.actions[aid].invariant:
                if not c.evaluate(state):
                    self.trace.append(
                        {"event": "prune_invariant", "t": t, "action": aid, "condition": c.describe()}
                    )
                    return None

        return _Node(
            t=t,
            state=state,
            renewable=renewable,
            consumable=consumable,
            alive=alive,
            ends=ends,
            counts=counts,
            schedule=node.schedule + (item,),
        )

    def _accept(self, node: _Node) -> None:
        cost = len(node.schedule)
        better = (
            self.best is None
            or cost < self.best_cost
            or (cost == self.best_cost and node.t < (self.goal_time if self.goal_time is not None else 10**9))
        )
        if better:
            self.best = node.schedule
            self.best_cost = cost
            self.goal_time = node.t
            self.trace.append(
                {
                    "event": "plan_found",
                    "nodes": self.nodes,
                    "cost": cost,
                    "goal_time": node.t,
                    "actions": [{"id": s.action_id, "start": s.start, "end": s.end} for s in node.schedule],
                }
            )

    # -- independent verification ------------------------------------------

    def _verify(self, schedule: tuple[ScheduledAction, ...]) -> SolveResult | None:
        """Replay the candidate through the independent simulator.

        Returns ``None`` on success, or a SolveResult carrying REPLAY_MISMATCH.
        """
        outcome = simulate(self.p, list(schedule))
        if not outcome.ok:
            err = outcome.error
            assert err is not None
            return SolveResult(
                status="invalid",
                failure_code=FailureCode.REPLAY_MISMATCH,
                schedule=schedule,
                nodes_expanded=self.nodes,
                best_cost=self.best_cost,
                goal_time=None,
                message=f"solver-produced plan failed independent replay at t={err.time}: {err.message}",
                trace=tuple(self.trace),
            )
        if not outcome.goal_met:
            return SolveResult(
                status="invalid",
                failure_code=FailureCode.REPLAY_MISMATCH,
                schedule=schedule,
                nodes_expanded=self.nodes,
                best_cost=self.best_cost,
                goal_time=None,
                message="solver-reported plan replays without error but the goal is not met",
                trace=tuple(self.trace),
            )
        return None


class _BudgetStop(Exception):
    pass
