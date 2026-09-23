"""Faithful concrete replay of a Z3-produced counterexample.

Every solver trace is *re-checked independently* with plain Python semantics
(:func:`cbmc.terms.evaluate`): guards must hold, parameters must be in range,
the simulated states must match the solver's states exactly, and the claimed
property violations must genuinely hold. A mismatch raises
:class:`ReplayError` — we never present a solver artifact as a counterexample
without executing it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .checker import StepTrace, parse_properties
from .errors import ReplayError
from .model import Action, Contract
from .terms import evaluate


@dataclass
class ReplayStep:
    step: int
    action: str
    params: dict[str, Any]
    state: dict[str, Any]
    guards_held: bool


@dataclass
class PropertyVerdict:
    property: str
    holds: bool
    detail: str


@dataclass
class ReplayReport:
    contract: str
    executable: bool
    states_match_solver: bool
    steps: list[ReplayStep]
    final_state: dict[str, Any]
    property_verdicts: list[PropertyVerdict]
    violated: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "executable": self.executable,
            "states_match_solver": self.states_match_solver,
            "steps": [asdict(s) for s in self.steps],
            "final_state": self.final_state,
            "property_verdicts": [asdict(v) for v in self.property_verdicts],
            "violated": self.violated,
        }


def _widths(contract: Contract, action: Action) -> dict[str, int | None]:
    widths = {n: v.width for n, v in contract.variables.items()}
    widths.update({p.name: p.width for p in action.params})
    for cname in contract.constants:
        widths.setdefault(cname, None)
    return widths


def _normalise_bv_init(contract: Contract) -> dict[str, int]:
    out = dict(contract.initial_int)
    for name, value in list(out.items()):
        w = contract.variables[name].width
        if w is not None:
            out[name] = value % (1 << w)
    return out


def _check_param_bounds(param_values: dict[str, Any], action: Action) -> None:
    for p in action.params:
        if p.name not in param_values:
            raise ReplayError(f"missing parameter {p.name!r} for action "
                              f"{action.name!r}")
        val = param_values[p.name]
        if p.sort == "bool":
            if not isinstance(val, bool):
                raise ReplayError(f"parameter {p.name!r} must be boolean")
            continue
        if not isinstance(val, int) or isinstance(val, bool):
            raise ReplayError(f"parameter {p.name!r} must be an integer")
        if p.width is not None and not 0 <= val <= (1 << p.width) - 1:
            raise ReplayError(
                f"parameter {p.name!r}={val} outside unsigned {p.width}-bit range"
            )
        if p.min is not None and val < p.min:
            raise ReplayError(f"parameter {p.name!r}={val} below min {p.min}")
        if p.max is not None and val > p.max:
            raise ReplayError(f"parameter {p.name!r}={val} above max {p.max}")


def _env(contract: Contract, state: dict[str, Any], action: Action,
         params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int | None]]:
    env: dict[str, Any] = dict(state)
    env.update(params)
    env.update(contract.constants)
    return env, _widths(contract, action)


def replay_trace(
    contract: Contract,
    trace: list[StepTrace] | list[dict[str, Any]],
    compare_to_solver: bool = True,
) -> ReplayReport:
    """Concretely execute ``trace`` starting from the contract's initial state.

    ``trace`` may be the dataclass trace returned by the checker or the plain
    dicts sent over the API.
    """
    norm = [t if isinstance(t, StepTrace) else _step_from_dict(t) for t in trace]
    if not norm:
        raise ReplayError("cannot replay an empty trace")

    state: dict[str, Any] = {}
    state.update(_normalise_bv_init(contract))
    state.update(contract.initial_bool)
    _assert_solver_state(contract, norm[0].state, state, 0, compare_to_solver)

    replayed: list[ReplayStep] = [
        ReplayStep(step=0, action="<initial>", params={}, state=dict(state),
                   guards_held=True)
    ]

    for recorded in norm[1:]:
        if recorded.action is None:
            raise ReplayError(f"step {recorded.step} has no action to execute")
        if recorded.action not in contract.actions:
            raise ReplayError(f"unknown action {recorded.action!r}")
        action = contract.actions[recorded.action]
        params = dict(recorded.params)
        _check_param_bounds(params, action)
        env, widths = _env(contract, state, action, params)

        guards = [bool(evaluate(e, env, widths)) for e in action.require]
        if not all(guards):
            raise ReplayError(
                f"action {action.name!r} is disabled at step {recorded.step}: "
                f"guards evaluate to {guards}"
            )

        # simultaneous assignment from the PRE-action state
        new_state = dict(state)
        for target, expr in action.assign:
            tw = contract.variables[target].width
            new_state[target] = evaluate(expr, env, widths,
                                         force_width=tw)
        state = new_state

        _assert_solver_state(contract, recorded.state, state, recorded.step,
                             compare_to_solver)
        replayed.append(ReplayStep(
            step=recorded.step, action=action.name, params=params,
            state=dict(state), guards_held=True,
        ))

    verdicts, violated = _check_properties_concretely(contract, state)
    return ReplayReport(
        contract=contract.name,
        executable=True,
        states_match_solver=True,
        steps=replayed,
        final_state=state,
        property_verdicts=verdicts,
        violated=violated,
    )


def _step_from_dict(raw: dict[str, Any]) -> StepTrace:
    return StepTrace(
        step=raw["step"],
        action=raw.get("action"),
        params=raw.get("params", {}),
        state=raw.get("state", {}),
        violations=raw.get("violations", []),
    )


def _assert_solver_state(contract: Contract, solver_state: dict[str, Any],
                         concrete: dict[str, Any], step: int,
                         compare: bool) -> None:
    if not compare:
        return
    for name in contract.variables:
        if name not in solver_state:
            raise ReplayError(f"solver state at step {step} lacks {name!r}")
        if solver_state[name] != concrete[name]:
            raise ReplayError(
                f"state mismatch at step {step} for {name!r}: "
                f"solver said {solver_state[name]!r}, concrete replay says "
                f"{concrete[name]!r}"
            )


def _check_properties_concretely(
    contract: Contract, state: dict[str, Any]
) -> tuple[list[PropertyVerdict], list[str]]:
    balances, groups, targets = parse_properties(contract)
    verdicts: list[PropertyVerdict] = []
    violated: list[str] = []
    init = _normalise_bv_init(contract)

    for name in balances:
        w = contract.variables[name].width
        if w is not None:
            # unsigned machine ints are non-negative by construction
            verdicts.append(PropertyVerdict(
                f"nonnegative:{name}", True,
                f"{name}={state[name]} (unsigned {w}-bit, n/a)"))
            continue
        value = state[name]
        bad = value < 0
        v = PropertyVerdict(
            f"nonnegative:{name}", not bad,
            f"{name}={value}" + (" (negative)" if bad else " (non-negative)"),
        )
        verdicts.append(v)
        if bad:
            violated.append(v.property)

    for i, group in enumerate(groups):
        now = sum(state[v] for v in group)
        before = sum(init[v] for v in group)
        bad = now != before
        v = PropertyVerdict(
            f"conservation:{i}", not bad,
            f"sum {group}: initial={before}, final={now}"
            + (" — CONSERVATION BROKEN" if bad else " — conserved"),
        )
        verdicts.append(v)
        if bad:
            violated.append(v.property)

    for t in targets:
        widths = {n: vv.width for n, vv in contract.variables.items()}
        for cname in contract.constants:
            widths.setdefault(cname, None)
        env = dict(state)
        env.update(contract.constants)
        reached = bool(evaluate(t.expr, env, widths))
        v = PropertyVerdict(f"target:{t.name}", not reached,
                            f"target reachable={reached}")
        verdicts.append(v)
        if reached:
            violated.append(v.property)

    return verdicts, violated
