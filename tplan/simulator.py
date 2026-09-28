"""Timeline simulation kernel.

``simulate`` takes a problem and a concrete schedule and independently
reconstructs the full timeline ``0..horizon``, checking:

1. **Start conditions** at each action's start instant (before its effects).
2. **Durational invariants** at *every* grid point of the half-open alive
   interval ``[start, end)`` -- including interior points -- not only at the
   two endpoints. A zero-duration action ``[t, t)`` has its invariant checked
   at the single instant *t*, after its effects.
3. **Effects** applied atomically at the end instant (state-changing instant
   for a positive-duration action).
4. **Renewable resource** occupancy over half-open ``[start, end)`` intervals
   (released exactly at ``end``) and **consumable** debit-at-start /
   credit-at-end.
5. Simultaneous events follow an explicit policy:

   * ``END`` of one action at *t* followed by ``START`` of another at *t* is
     **ordered** (end effects + release first, then start) -- this is the
     boundary-release hand-off and is always legal.
   * Two starts, two ends, or any event colliding with a zero-duration
     ``TICK`` at the same instant is **rejected** with
     ``SIMULTANEOUS_CONFLICT`` (the outcome would be ambiguous).

The simulator is deliberately self-contained (it does not import the
solver); the test-suite uses it as the independent replay oracle.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import groupby
from typing import Mapping

from .model import (
    ActionSpec,
    EventKind,
    FailureCode,
    Problem,
    ResourceKind,
    ScheduledAction,
    TimelineEvent,
)


class SimulationError(Exception):
    """A rejected schedule. ``code`` is one of ``FailureCode``."""

    def __init__(self, code: FailureCode, message: str, time: int | None = None):
        super().__init__(message)
        self.code = code
        self.time = time
        self.message = message


@dataclass(frozen=True)
class _Evt:
    time: int
    phase: int  # 0 = END (effects+release), 1 = TICK, 2 = START (check+debit)
    seq: int  # tie-break: action index in problem.action_order
    kind: EventKind
    action: ActionSpec


@dataclass
class _Res:
    renewable: dict[str, int]
    consumable: dict[str, int]
    capacity: dict[str, int]


@dataclass
class SimulationOutcome:
    ok: bool
    events: tuple[TimelineEvent, ...]
    states: dict[int, dict[str, str]]  # state AFTER all events at that time
    goal_met: bool
    error: SimulationError | None = None


def _snapshot_state(state: Mapping[str, Fraction]) -> dict[str, str]:
    return {k: str(v) for k, v in sorted(state.items())}


def _snapshot_res(res: _Res) -> dict[str, int]:
    out: dict[str, int] = {}
    for rid in sorted(set(res.renewable) | set(res.consumable)):
        out[rid] = res.renewable.get(rid, res.consumable.get(rid, 0))
    return out


def _fail(conds: tuple, state: Mapping[str, Fraction], kind: str, t: int, aid: str) -> None:
    for c in conds:
        if not c.evaluate(state):
            raise SimulationError(
                FailureCode.INVARIANT_VIOLATED
                if kind == "invariant"
                else FailureCode.START_CONDITION_VIOLATED,
                f"action {aid!r} {kind} violated at t={t}: {c.describe()}",
                time=t,
            )


def simulate(problem: Problem, schedule: list[ScheduledAction]) -> SimulationOutcome:
    """Replay ``schedule`` against ``problem`` and return the full timeline."""

    timeline: list[TimelineEvent] = []
    try:
        events = _build_events(problem, schedule)
        state = dict(problem.fluents)
        res = _Res(
            renewable={
                rid: 0 for rid, r in problem.resources.items() if r.kind is ResourceKind.RENEWABLE
            },
            consumable={
                rid: r.capacity
                for rid, r in problem.resources.items()
                if r.kind is ResourceKind.CONSUMABLE
            },
            capacity={rid: r.capacity for rid, r in problem.resources.items()},
        )
        timeline.append(
            TimelineEvent(
                time=0,
                order=0,
                kind=EventKind.INIT,
                action_id="-",
                state=_snapshot_state(state),
                resources=_snapshot_res(res),
                note="initial state",
            )
        )
        order = 1
        for t, grp in groupby(events, key=lambda e: e.time):
            for e in grp:  # grp already in deterministic total order
                _apply_event(e, state, res, t, order, timeline)
                order += 1

        # Per-instant state index built from the event snapshots, then
        # forward-filled to every grid point 0..horizon (state is constant
        # between event instants).
        event_states: dict[int, dict[str, str]] = {}
        for ev in timeline:
            event_states[ev.time] = dict(ev.state)
        states_by_time: dict[int, dict[str, str]] = {}
        current: dict[str, str] = event_states[0]
        for t in range(problem.horizon + 1):
            if t in event_states:
                current = event_states[t]
            states_by_time[t] = dict(current)

        # Independent interior audit: invariant at every grid point alive.
        _audit_invariants(problem, schedule, states_by_time)

        last_t = max(states_by_time)
        final_state = {k: Fraction(v) for k, v in states_by_time[last_t].items()}
        return SimulationOutcome(
            ok=True,
            events=tuple(timeline),
            states=states_by_time,
            goal_met=problem.goal.evaluate(final_state),
        )
    except SimulationError as exc:
        return SimulationOutcome(
            ok=False, events=tuple(timeline), states={}, goal_met=False, error=exc
        )


def _build_events(problem: Problem, schedule: list[ScheduledAction]) -> list[_Evt]:
    starts: dict[int, str] = {}
    ends: dict[int, str] = {}
    ticks: dict[int, str] = {}
    evts: list[_Evt] = []
    order_index = {aid: i for i, aid in enumerate(problem.action_order)}

    for sa in schedule:
        if sa.action_id not in problem.actions:
            raise SimulationError(
                FailureCode.INVALID_PROBLEM,
                f"schedule references unknown action {sa.action_id!r}",
                time=sa.start,
            )
        act = problem.actions[sa.action_id]
        if sa.end != sa.start + act.duration:
            raise SimulationError(
                FailureCode.INVALID_PROBLEM,
                f"action {sa.action_id!r}: end {sa.end} != start {sa.start} "
                f"+ duration {act.duration}",
                time=sa.start,
            )
        if not (0 <= sa.start <= problem.horizon and 0 <= sa.end <= problem.horizon):
            raise SimulationError(
                FailureCode.INVALID_PROBLEM,
                f"action {sa.action_id!r} outside horizon [0,{problem.horizon}]",
                time=sa.start,
            )

        if act.duration == 0:
            if sa.start in ticks:
                raise SimulationError(
                    FailureCode.SIMULTANEOUS_CONFLICT,
                    f"two zero-duration actions at t={sa.start}: "
                    f"{ticks[sa.start]!r} and {sa.action_id!r}",
                    time=sa.start,
                )
            ticks[sa.start] = sa.action_id
            evts.append(_Evt(sa.start, 1, order_index[sa.action_id], EventKind.TICK, act))
            continue

        if sa.start in starts:
            raise SimulationError(
                FailureCode.SIMULTANEOUS_CONFLICT,
                f"two action starts at t={sa.start}: {starts[sa.start]!r} "
                f"and {sa.action_id!r}",
                time=sa.start,
            )
        if sa.end in ends:
            raise SimulationError(
                FailureCode.SIMULTANEOUS_CONFLICT,
                f"two action ends at t={sa.end}: {ends[sa.end]!r} "
                f"and {sa.action_id!r}",
                time=sa.end,
            )
        starts[sa.start] = sa.action_id
        ends[sa.end] = sa.action_id
        evts.append(_Evt(sa.start, 2, order_index[sa.action_id], EventKind.START, act))
        evts.append(_Evt(sa.end, 0, order_index[sa.action_id], EventKind.END, act))

    # TICK colliding with any start/end at the same instant is ambiguous.
    for t, aid in ticks.items():
        if t in starts or t in ends:
            other = starts.get(t) or ends.get(t)
            raise SimulationError(
                FailureCode.SIMULTANEOUS_CONFLICT,
                f"zero-duration action {aid!r} collides with event of "
                f"{other!r} at t={t}",
                time=t,
            )

    evts.sort(key=lambda e: (e.time, e.phase, e.seq))
    return evts


def _apply_event(
    e: _Evt,
    state: dict[str, Fraction],
    res: _Res,
    t: int,
    order: int,
    timeline: list[TimelineEvent],
) -> None:
    act = e.action
    if e.kind is EventKind.END:
        _apply_effects(act, state, t)
        for rid, qty in act.resource_use.items():
            if rid in res.renewable:
                res.renewable[rid] -= qty
            else:
                res.consumable[rid] += qty
        timeline.append(
            _snap(t, order, e, state, res, "effects applied; resources released at end (half-open)")
        )
        return

    if e.kind is EventKind.TICK:
        _fail(act.start_condition, state, "start_condition", t, act.id)
        for rid, qty in act.resource_use.items():
            _debit(res, rid, qty, act.id, t)
        _apply_effects(act, state, t)
        for rid, qty in act.resource_use.items():
            if rid in res.renewable:
                res.renewable[rid] -= qty
            else:
                res.consumable[rid] += qty
        _fail(act.invariant, state, "invariant", t, act.id)
        timeline.append(
            _snap(t, order, e, state, res, "zero-duration: condition, debit, effects, release at one instant")
        )
        return

    # START
    _fail(act.start_condition, state, "start_condition", t, act.id)
    for rid, qty in act.resource_use.items():
        _debit(res, rid, qty, act.id, t)
    timeline.append(
        _snap(t, order, e, state, res, "start condition checked; resources debited")
    )


def _debit(res: _Res, rid: str, qty: int, aid: str, t: int) -> None:
    if rid in res.renewable:
        new_level = res.renewable[rid] + qty
        if new_level > res.capacity[rid]:
            raise SimulationError(
                FailureCode.RESOURCE_CONFLICT,
                f"action {aid!r}: renewable {rid!r} usage {new_level} exceeds "
                f"capacity {res.capacity[rid]} at t={t}",
                time=t,
            )
        res.renewable[rid] = new_level
    else:
        if res.consumable[rid] - qty < 0:
            raise SimulationError(
                FailureCode.RESOURCE_CONFLICT,
                f"action {aid!r}: consumable {rid!r} would go negative at t={t}",
                time=t,
            )
        res.consumable[rid] -= qty


def _apply_effects(act: ActionSpec, state: dict[str, Fraction], t: int) -> None:
    for eff in act.effects:
        state[eff.fluent] = eff.apply(state[eff.fluent])


def _snap(
    t: int, order: int, e: _Evt, state: Mapping[str, Fraction], res: _Res, note: str
) -> TimelineEvent:
    return TimelineEvent(
        time=t,
        order=order,
        kind=e.kind,
        action_id=e.action.id,
        state=_snapshot_state(state),
        resources=_snapshot_res(res),
        note=note,
    )


def _audit_invariants(
    problem: Problem,
    schedule: list[ScheduledAction],
    states_by_time: dict[int, dict[str, str]],
) -> None:
    """Re-check each action's invariant at every grid point it is alive.

    Positive-duration action: every t in the half-open window [start, end).
    Zero-duration action: the single instant t=start=end, after its effects.

    State between event instants is constant (effects exist only at events),
    so the state holding at grid point t is the state after the latest event
    at or before t.
    """
    event_times = sorted(states_by_time)
    for sa in schedule:
        act = problem.actions[sa.action_id]
        if not act.invariant:
            continue
        if act.duration == 0:
            points = (sa.start,)
        else:
            points = range(sa.start, sa.end)
        for t in points:
            latest = event_times[0]
            for et in event_times:
                if et <= t:
                    latest = et
                else:
                    break
            snap = {k: Fraction(v) for k, v in states_by_time[latest].items()}
            for c in act.invariant:
                if not c.evaluate(snap):
                    raise SimulationError(
                        FailureCode.INVARIANT_VIOLATED,
                        f"action {act.id!r} invariant violated at t={t} "
                        f"(alive window [{sa.start},{sa.end})): {c.describe()}",
                        time=t,
                    )
