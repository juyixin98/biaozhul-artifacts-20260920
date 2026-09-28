"""Independent timeline replay / validation engine.

This module is the semantic reference point of the whole system: both the
brute-force reference enumerator and the budgeted DFS solver judge a
candidate schedule exclusively through :func:`replay`. The test-suite
oracle re-implements the same event loop independently (it does not call
this module), and differential tests require both to agree.

Time semantics (integer grid, horizon ``H``; every action ends by ``H``):

at each grid time ``t`` the following fixed phases run, so events that
happen "simultaneously" have an explicit, documented order:

  A. END  -- effects of positive-duration actions ending exactly at t are
             applied (in declaration order). Write/write clashes between
             distinct actions at the same instant are rejected upfront
             with SIMULTANEOUS_CONFLICT instead of being silently ordered.
  B. PRE  -- preconditions of every action starting at t (positive and
             zero duration) are read against the same post-end state.
             A starter therefore sees end effects released at t
             ("ends first"), but never effects of zero-duration actions
             firing at t.
  C. ZERO -- effects of zero-duration actions starting at t are applied.
  D. INV  -- duration invariants of every action active on segment
             ``[t, t+1)`` (i.e. start <= t < end) are checked against the
             resulting segment state. Invariants are checked at EVERY
             interior grid point start..end-1, so a condition knocked
             down mid-action by another action's end effect is detected;
             they are never checked only at the two endpoints. A
             zero-duration action has no interior points: its invariant
             is vacuous.

Resources are a separate, purely interval-based concern: every
occupation is the half-open interval ``[start, end)``, so ``[s,t)`` and
``[t,e)`` never conflict (boundary release) and ``[t,t)`` (zero
duration) holds nothing at all.

The goal is evaluated in the segment state at H after phase A (no action
may still run at H).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..rules.conditions import (
    apply_effects,
    effect_facts,
    evaluate,
    referenced_facts,
)
from ..rules.errors import FailureCategory
from ..rules.models import (
    Action,
    CheckOutcome,
    EventKind,
    Plan,
    Problem,
    ReplayResult,
    ScheduledAction,
    TimelineEvent,
    Violation,
)
from ..rules.time import Interval, overlap_point


@dataclass(frozen=True)
class _Bound:
    """A scheduled action bound to its static definition."""

    step: ScheduledAction
    action: Action
    decl_index: int

    @property
    def interval(self) -> Interval:
        return Interval(self.step.start, self.step.end)


def _decl_order(problem: Problem) -> dict[str, int]:
    return {action.name: i for i, action in enumerate(problem.actions)}


def _bind_steps(problem: Problem, plan: Plan) -> tuple[list[_Bound], list[Violation]]:
    bindings: list[_Bound] = []
    violations: list[Violation] = []
    order = _decl_order(problem)
    definitions = {action.name: action for action in problem.actions}
    for index, step in enumerate(plan.steps):
        action = definitions.get(step.action)
        if action is None:
            violations.append(
                Violation(
                    category=FailureCategory.SCHEDULE_INVALID,
                    time=step.start,
                    action=step.action,
                    message=f"step[{index}] references unknown action {step.action!r}",
                )
            )
            continue
        local_violations: list[Violation] = []
        if not action.duration_min <= step.duration <= (action.duration_max or action.duration_min):
            local_violations.append(
                Violation(
                    category=FailureCategory.SCHEDULE_INVALID,
                    time=step.start,
                    action=step.action,
                    message=(
                        f"duration {step.duration} outside declared range "
                        f"[{action.duration_min}, {action.duration_max}]"
                    ),
                )
            )
        if step.end > problem.horizon:
            local_violations.append(
                Violation(
                    category=FailureCategory.SCHEDULE_INVALID,
                    time=step.end,
                    action=step.action,
                    message=f"action ends at {step.end} beyond horizon {problem.horizon}",
                )
            )
        violations.extend(local_violations)
        # Keep the binding even when malformed so later phases can still
        # produce evidence; the plan is already invalid at this point.
        bindings.append(_Bound(step=step, action=action, decl_index=order[step.action]))
    return bindings, violations


def _check_resources(bound: list[_Bound]) -> list[Violation]:
    violations: list[Violation] = []
    for i, first in enumerate(bound):
        if first.interval.is_empty:
            continue  # [t, t) holds nothing -- boundary release / zero duration
        for second in bound[i + 1 :]:
            if second.interval.is_empty:
                continue
            shared = set(first.action.resources) & set(second.action.resources)
            if not shared:
                continue
            point = overlap_point(first.interval, second.interval)
            if point is not None:
                for resource in sorted(shared):
                    violations.append(
                        Violation(
                            category=FailureCategory.RESOURCE_CONFLICT,
                            time=point,
                            resource=resource,
                            action=first.step.action,
                            message=(
                                f"resource {resource!r} held by overlapping half-open intervals "
                                f"{first.step.action}[{first.step.start},{first.step.end}) and "
                                f"{second.step.action}[{second.step.start},{second.step.end})"
                            ),
                            detail=f"conflicts with {second.step.action}",
                        )
                    )
    return violations


@dataclass(frozen=True)
class _Write:
    time: int
    fact: str
    owner: str
    phase: str  # "END" or "ZERO"


def _check_simultaneous_writes(bound: list[_Bound]) -> tuple[list[Violation], dict[int, dict[str, str]]]:
    """Reject same-instant write/write pairs across action instances.

    Two distinct scheduled *instances* writing one fact at one instant are
    rejected, even when they share an action declaration, because no
    ordering can distinguish them. Exactly one violation is emitted per
    ``(time, fact)`` pair, listing every writing action.

    Returns violations and the accepted ``time -> fact -> owner`` map.
    """
    violations: list[Violation] = []
    writers: dict[tuple[int, str], list[_Write]] = {}
    for item in bound:
        phase = "ZERO" if item.step.duration == 0 else "END"
        when = item.step.start if item.step.duration == 0 else item.step.end
        for fact in sorted(effect_facts(item.action.effects)):
            writers.setdefault((when, fact), []).append(
                _Write(time=when, fact=fact, owner=item.step.action, phase=phase)
            )
    accepted: dict[int, dict[str, str]] = {}
    for (when, fact), writes in sorted(writers.items()):
        ordered = sorted(writes, key=lambda w: (w.owner, w.phase))
        if len(ordered) > 1:
            owners = sorted({w.owner for w in ordered})
            phases = sorted({w.phase for w in ordered})
            violations.append(
                Violation(
                    category=FailureCategory.SIMULTANEOUS_CONFLICT,
                    time=when,
                    action=owners[0],
                    resource=fact,
                    message=(
                        f"fact {fact!r} written simultaneously at t={when} by "
                        f"{len(ordered)} instance(s) of actions {owners} (phases {phases}); "
                        "policy ENDS_FIRST_REJECT_WRITE_WRITES rejects ambiguous write order"
                    ),
                    detail=f"conflicts with {owners[1:]}",
                )
            )
        else:
            accepted.setdefault(when, {})[fact] = ordered[0].owner
    return violations, accepted


def _snapshot(condition: object, state: dict[str, object]) -> str:
    try:
        facts = sorted(referenced_facts(condition))
    except Exception:  # pragma: no cover - conditions are pre-validated
        return "condition=<unparseable>"
    if not facts:
        return "condition=always"
    return ", ".join(f"{fact}={state.get(fact, 0)!r}" for fact in facts)


def replay(problem: Problem, plan: Plan) -> ReplayResult:
    """Replay ``plan`` against ``problem`` and return full evidence.

    The result is never an exception for ordinary plan defects: every
    defect becomes a typed :class:`Violation`; ``outcome`` is INVALID
    exactly when at least one violation exists.
    """
    bound, violations = _bind_steps(problem, plan)
    violations.extend(_check_resources(bound))
    write_violations, _accepted_writes = _check_simultaneous_writes(bound)
    violations.extend(write_violations)

    events: list[TimelineEvent] = []
    order_counter = 0

    def emit(
        time: int,
        kind: EventKind,
        *,
        action: str | None,
        detail: str | None,
        before: dict[str, object],
        after: dict[str, object],
        active: list[str],
    ) -> None:
        nonlocal order_counter
        events.append(
            TimelineEvent(
                time=time,
                order=order_counter,
                kind=kind,
                action=action,
                detail=detail,
                state_before=dict(before),
                state_after=dict(after),
                active_actions=list(active),
            )
        )
        order_counter += 1

    def active_at(time: int) -> list[str]:
        names = [
            item.step.action
            for item in bound
            if item.step.start <= time < item.step.end and item.step.duration > 0
        ]
        return sorted(names, key=lambda name: (_decl_order(problem)[name], name))

    state: dict[str, object] = dict(problem.initial)
    emit(0, EventKind.INITIAL, action=None, detail="initial state", before={}, after=state, active=[])

    by_end: dict[int, list[_Bound]] = {}
    by_start: dict[int, list[_Bound]] = {}
    for item in sorted(bound, key=lambda b: b.decl_index):
        if item.step.duration > 0:
            by_end.setdefault(item.step.end, []).append(item)
        by_start.setdefault(item.step.start, []).append(item)

    horizon = problem.horizon
    for t in range(0, horizon + 1):
        # Phase A: end effects of positive-duration actions (declaration order).
        for item in by_end.get(t, []):
            before = dict(state)
            state = apply_effects(item.action.effects, state)
            emit(
                t,
                EventKind.END,
                action=item.step.action,
                detail=f"apply end effects: {_effects_label(item.action.effects)}",
                before=before,
                after=state,
                active=active_at(t),
            )

        # Phase B: preconditions of every starter at t (uniform post-end state).
        starters = sorted(by_start.get(t, []), key=lambda b: b.decl_index)
        for item in starters:
            ok = evaluate(item.action.precondition, state)
            if not ok:
                violations.append(
                    Violation(
                        category=FailureCategory.PRECONDITION_VIOLATION,
                        time=t,
                        action=item.step.action,
                        message=f"start precondition failed at t={t}",
                        detail=f"snapshot({_snapshot(item.action.precondition, state)})",
                    )
                )
            emit(
                t,
                EventKind.START,
                action=item.step.action,
                detail=(
                    f"start precondition at t={t}: {'OK' if ok else 'FAILED'} "
                    f"(duration {item.step.duration}, occupation [{t},{t + item.step.duration})) "
                    f"| {_snapshot(item.action.precondition, state)}"
                ),
                before=state,
                after=state,
                active=active_at(t),
            )

        # Phase C: zero-duration action effects (declaration order).
        for item in starters:
            if item.step.duration != 0:
                continue
            before = dict(state)
            state = apply_effects(item.action.effects, state)
            emit(
                t,
                EventKind.ZERO_DURATION,
                action=item.step.action,
                detail=f"zero-duration action effects: {_effects_label(item.action.effects)}",
                before=before,
                after=state,
                active=active_at(t),
            )

        # Phase D: invariants on segment [t, t+1), checked at EVERY interior
        # point start..end-1 against the final segment state (decl order).
        segment_active = sorted(
            [b for b in bound if b.step.start <= t < b.step.end and b.step.duration > 0],
            key=lambda b: b.decl_index,
        )
        for item in segment_active:
            ok = evaluate(item.action.invariant, state)
            if not ok:
                violations.append(
                    Violation(
                        category=FailureCategory.INVARIANT_VIOLATION,
                        time=t,
                        action=item.step.action,
                        message=f"duration invariant failed on segment [t,t+1) at t={t}",
                        detail=(
                            f"interval [{item.step.start},{item.step.end}); "
                            f"snapshot({_snapshot(item.action.invariant, state)})"
                        ),
                    )
                )
            emit(
                t,
                EventKind.INVARIANT_CHECK,
                action=item.step.action,
                detail=(
                    f"invariant on segment [{t},{t + 1}): {'OK' if ok else 'FAILED'} "
                    f"| {_snapshot(item.action.invariant, state)}"
                ),
                before=state,
                after=state,
                active=[b.step.action for b in segment_active],
            )

    goal_satisfied = evaluate(problem.goal, state)
    if not goal_satisfied:
        violations.append(
            Violation(
                category=FailureCategory.GOAL_NOT_REACHED,
                time=horizon,
                message=f"goal not satisfied in final state at horizon t={horizon}",
                detail=f"snapshot({_snapshot(problem.goal, state)})",
            )
        )
    emit(
        horizon,
        EventKind.HORIZON,
        action=None,
        detail=f"goal evaluated: {'SATISFIED' if goal_satisfied else 'UNSATISFIED'} "
        f"| {_snapshot(problem.goal, state)}",
        before=state,
        after=state,
        active=[],
    )

    return ReplayResult(
        outcome=CheckOutcome.VALID if not violations else CheckOutcome.INVALID,
        goal_satisfied=goal_satisfied,
        events=events,
        violations=sorted(violations, key=_violation_key),
        final_state=dict(state),
        horizon=horizon,
    )


def _violation_key(violation: Violation) -> tuple:
    return (
        violation.time,
        violation.category,
        violation.action or "",
        violation.resource or "",
        violation.message,
    )


def _effects_label(effects: object) -> str:
    if not isinstance(effects, list) or not effects:
        return "none"
    parts = []
    for raw in effects:
        parts.append(f"{raw.get('fact')}{raw.get('op', '=')}{raw.get('value')!r}")
    return ", ".join(parts)
