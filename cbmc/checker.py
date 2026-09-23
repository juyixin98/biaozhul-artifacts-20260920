"""Bounded model checking of JSON contracts with Z3.

Given a bound :math:`K`, the checker builds the unrolled transition system

* fresh state symbols for every step 0..K,
* a concrete initial state,
* one action selector + fresh parameter symbols per transition,

and asks (existentially) whether a property can be violated at step *k*.

Depths are tried in order ``k = 0..K``, so the first ``SAT`` answer is a
**shortest** counterexample. Results never claim unbounded safety:

* ``counterexample``      - SAT at some k <= K, trace attached;
* ``safe_within_bound``   - UNSAT at every k <= K (nothing more is implied);
* ``timeout``             - Z3 hit the time limit at some depth; shorter depths
                            were proven clean, this one and beyond are unknown;
* ``unknown``             - Z3 gave up for another reason (memory, quantifiers,
                            undecidable nonlinear arithmetic, ...).
"""

from __future__ import annotations

import enum
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .errors import ContractError
from .model import Action, Contract, Param, Var
from .terms import SymbolicBuilder, sort_expr

MAX_BOUND = 1000


class Status(str, enum.Enum):
    COUNTEREXAMPLE = "counterexample"
    SAFE_WITHIN_BOUND = "safe_within_bound"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass
class StepTrace:
    """One state on a counterexample (state 0 is the concrete initial state)."""

    step: int
    action: str | None
    params: dict[str, Any]
    state: dict[str, Any]
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Violation:
    property: str          # "nonnegative" | "conservation:<i>" | "target:<name>"
    detail: str


@dataclass
class CheckResult:
    status: Status
    contract: str
    bound: int
    depth: int | None
    violation: str | None
    detail: str | None
    trace: list[StepTrace]
    elapsed_ms: int
    solver_calls: int
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "contract": self.contract,
            "bound": self.bound,
            "depth": self.depth,
            "violation": self.violation,
            "detail": self.detail,
            "trace": [s.to_dict() for s in self.trace],
            "elapsed_ms": self.elapsed_ms,
            "solver_calls": self.solver_calls,
            "note": self.note,
        }


# --------------------------------------------------------------------------- #
# Property configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Target:
    name: str
    expr: dict[str, Any]


@dataclass(frozen=True)
class CheckConfig:
    steps: int
    timeout_ms: int = 10_000          # per single solver call
    checks: tuple[str, ...] = ("nonnegative", "conservation", "targets")

    def validated(self) -> "CheckConfig":
        if not isinstance(self.steps, int) or isinstance(self.steps, bool):
            raise ContractError("steps must be an integer")
        if not 0 <= self.steps <= MAX_BOUND:
            raise ContractError(f"steps must be in [0, {MAX_BOUND}]")
        if not isinstance(self.timeout_ms, int) or self.timeout_ms <= 0:
            raise ContractError("timeout_ms must be a positive integer")
        allowed = {"nonnegative", "conservation", "targets"}
        bad = set(self.checks) - allowed
        if bad:
            raise ContractError(f"unknown checks: {sorted(bad)}")
        return self


def parse_properties(contract: Contract) -> tuple[list[str], list[list[str]],
                                                  list[Target]]:
    """Extract (balance vars, conservation groups, named targets) from JSON."""
    props = contract.raw.get("properties", {})
    if not isinstance(props, dict):
        raise ContractError("properties must be an object", "properties")

    int_vars = set(contract.int_vars)
    balances = props.get("balances", sorted(int_vars))
    if not isinstance(balances, list) or not all(isinstance(v, str) for v in balances):
        raise ContractError("properties.balances must be a list of names",
                            "properties.balances")
    for v in balances:
        if v not in contract.variables:
            raise ContractError(f"unknown balance variable {v!r}",
                                "properties.balances")
        if contract.variables[v].sort != "int":
            raise ContractError(f"balance {v!r} must be an integer variable",
                                "properties.balances")

    groups_raw = props.get("conservation", [sorted(int_vars)])
    if (not isinstance(groups_raw, list)
            or not all(isinstance(g, list) for g in groups_raw)):
        raise ContractError(
            "properties.conservation must be a list of lists of names",
            "properties.conservation",
        )
    groups: list[list[str]] = []
    for i, group in enumerate(groups_raw):
        if not group or not all(isinstance(v, str) for v in group):
            raise ContractError("conservation groups must be non-empty name lists",
                                f"properties.conservation[{i}]")
        for v in group:
            if v not in int_vars:
                raise ContractError(
                    f"unknown/integer-only conservation variable {v!r}",
                    f"properties.conservation[{i}]",
                )
        widths = {contract.variables[v].width for v in group}
        if len(widths) > 1:
            raise ContractError(
                f"conservation group {i} mixes math ints and bit-vectors; "
                "keep one width class per group",
                f"properties.conservation[{i}]",
            )
        groups.append(list(group))

    targets: list[Target] = []
    for i, item in enumerate(props.get("targets", [])):
        path = f"properties.targets[{i}]"
        if not isinstance(item, dict) or "name" not in item or "expr" not in item:
            raise ContractError("target needs 'name' and 'expr'", path)
        name = item["name"]
        if not isinstance(name, str):
            raise ContractError("target name must be a string", path)
        scope = dict(contract.variables)
        for cname in contract.constants:
            scope.setdefault(cname, Var(cname, "int"))
        sort_expr(item["expr"], scope, f"{path}.expr", expected="bool")
        targets.append(Target(name, item["expr"]))

    return balances, groups, targets


# --------------------------------------------------------------------------- #
# Symbolic unrolling
# --------------------------------------------------------------------------- #

def _fresh_state(contract: Contract, k: int) -> dict[str, Any]:
    import z3

    sym: dict[str, Any] = {}
    for name, var in contract.variables.items():
        tag = f"s{k}__{name}"
        if var.sort == "bool":
            sym[name] = z3.Bool(tag)
        elif var.width is not None:
            sym[name] = z3.BitVec(tag, var.width)
        else:
            sym[name] = z3.Int(tag)
    return sym


def _param_symbol(param: Param, tag: str) -> Any:
    import z3

    if param.sort == "bool":
        return z3.Bool(tag)
    if param.width is not None:
        return z3.BitVec(tag, param.width)
    return z3.Int(tag)


def _widths_table(contract: Contract, action: Action) -> dict[str, int | None]:
    widths = {n: v.width for n, v in contract.variables.items()}
    widths.update({p.name: p.width for p in action.params})
    for cname in contract.constants:
        widths.setdefault(cname, None)
    return widths


def _initial_constraints(solver: Any, contract: Contract,
                         state: dict[str, Any]) -> None:
    import z3

    for name, value in contract.initial_int.items():
        var = contract.variables[name]
        if var.width is not None:
            solver.add(state[name] == z3.BitVecVal(value % (1 << var.width),
                                                    var.width))
        else:
            solver.add(state[name] == z3.IntVal(value))
    for name, value in contract.initial_bool.items():
        solver.add(state[name] == z3.BoolVal(value))


def _sum_expr(contract: Contract, group: list[str], state: dict[str, Any],
              extraw: int = 64) -> Any:
    import z3

    width = contract.variables[group[0]].width
    if width is None:
        return z3.Sum(*[state[v] for v in group])
    terms = [z3.ZeroExt(extraw, state[v]) for v in group]
    total = terms[0]
    for t in terms[1:]:
        total = total + t
    return total


def _initial_sum(contract: Contract, group: list[str], extraw: int = 64) -> Any:
    import z3

    width = contract.variables[group[0]].width
    if width is None:
        return z3.IntVal(sum(contract.initial_int[v] for v in group))
    widened = width + extraw
    total = sum(contract.initial_int[v] % (1 << width) for v in group)
    return z3.BitVecVal(total % (1 << widened), widened)


def _bad_predicates(
    contract: Contract, balances: list[str], groups: list[list[str]],
    targets: list[Target], state: dict[str, Any],
) -> list[tuple[Any, Violation]]:
    import z3

    out: list[tuple[Any, Violation]] = []
    for v in balances:
        var = contract.variables[v]
        if var.width is not None:
            # Unsigned machine integers are non-negative by construction;
            # overflow of an unsigned balance is observed through conservation
            # (widened sum), not through a sign test.
            continue
        pred = state[v] < 0
        out.append((pred, Violation(
            "nonnegative", f"balance {v!r} went negative")))
    for i, group in enumerate(groups):
        pred = _sum_expr(contract, group, state) != _initial_sum(contract, group)
        out.append((pred, Violation(
            f"conservation:{i}",
            f"sum of {group} changed relative to the initial total")))
    for t in targets:
        builder = SymbolicBuilder({n: vv.width for n, vv in
                                   contract.variables.items()})
        sym = dict(state)
        for cname, cval in contract.constants.items():
            sym[cname] = z3.IntVal(cval)
        out.append((builder.build(t.expr, sym),
                    Violation(f"target:{t.name}",
                              f"named target {t.name!r} is reachable")))
    return out


def _as_unsigned(model: Any, value: Any, width: int | None) -> Any:
    import z3

    concrete = model.eval(value, model_completion=True)
    if z3.is_bv(value):
        return concrete.as_long() & ((1 << width) - 1)
    if z3.is_bool(value):
        return z3.is_true(concrete)
    return concrete.as_long()


def _extract_trace(
    contract: Contract, states: list[dict[str, Any]], model: Any,
    depth: int, selectors: list[Any], violation: Violation,
) -> list[StepTrace]:
    import z3

    trace: list[StepTrace] = []
    for k in range(depth + 1):
        state_values: dict[str, Any] = {}
        for name in contract.bool_vars:
            state_values[name] = bool(z3.is_true(
                model.eval(states[k][name], model_completion=True)))
        for name in contract.int_vars:
            state_values[name] = _as_unsigned(
                model, states[k][name], contract.variables[name].width)

        action_name: str | None = None
        param_values: dict[str, Any] = {}
        violations: list[str] = []
        if k > 0:
            # selectors[k-1] is the transition that ARRIVED at state k
            idx = model.eval(selectors[k - 1], model_completion=True).as_long()
            action_name = sorted(contract.actions)[idx]
            action = contract.actions[action_name]
            for p in action.params:
                tag = f"s{k - 1}__a{idx}__{p.name}"
                sym = z3.BitVec(tag, p.width) if p.width is not None else (
                    z3.Bool(tag) if p.sort == "bool" else z3.Int(tag))
                if p.sort == "bool":
                    param_values[p.name] = bool(
                        z3.is_true(model.eval(sym, model_completion=True)))
                else:
                    param_values[p.name] = _as_unsigned(model, sym, p.width)
        if k == depth:
            violations = [f"{violation.property}: {violation.detail}"]

        trace.append(StepTrace(
            step=k, action=action_name, params=param_values,
            state=state_values, violations=violations,
        ))
    return trace


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def check_contract(contract: Contract, config: CheckConfig) -> CheckResult:
    import z3

    config.validated()
    balances, groups, targets = parse_properties(contract)
    if "nonnegative" not in config.checks:
        balances = []
    if "conservation" not in config.checks:
        groups = []
    if "targets" not in config.checks:
        targets = []

    start = time.monotonic()
    solver_calls = 0

    # states/selectors are created once; the solver accumulates transition
    # constraints incrementally and each depth adds just one new unrolled step.
    states = [_fresh_state(contract, k) for k in range(config.steps + 1)]
    selectors = [z3.Int(f"s{k}__action") for k in range(config.steps)]

    solver = z3.Solver()
    solver.set("timeout", config.timeout_ms)
    _initial_constraints(solver, contract, states[0])

    for depth in range(config.steps + 1):
        if depth > 0:
            _add_transition_with(solver, contract, states, depth - 1, selectors)

        bad = _bad_predicates(contract, balances, groups, targets,
                              states[depth])
        if not bad:
            return CheckResult(
                status=Status.SAFE_WITHIN_BOUND,
                contract=contract.name, bound=config.steps, depth=None,
                violation=None, detail="no properties enabled for checking",
                trace=[],
                elapsed_ms=int((time.monotonic() - start) * 1000),
                solver_calls=solver_calls,
                note="Nothing was checked; enable at least one check.",
            )

        # assert the reachable-bad query for THIS depth, then retract it
        solver.push()
        solver.add(z3.Or(*[pred for pred, _ in bad]))
        solver_calls += 1
        answer = solver.check()
        elapsed = int((time.monotonic() - start) * 1000)

        if answer == z3.sat:
            model = solver.model()
            solver.pop()
            # identify which disjunct fired
            fired: Violation | None = None
            for pred, viol in bad:
                if z3.is_true(model.eval(pred, model_completion=True)):
                    fired = viol
                    break
            assert fired is not None
            trace = _extract_trace(contract, states[: depth + 1], model,
                                   depth, selectors[:depth], fired)
            return CheckResult(
                status=Status.COUNTEREXAMPLE,
                contract=contract.name, bound=config.steps, depth=depth,
                violation=fired.property, detail=fired.detail, trace=trace,
                elapsed_ms=elapsed, solver_calls=solver_calls,
                note=(f"Counterexample found at step {depth}. This says "
                      "nothing about executions longer than "
                      f"{config.steps} steps."),
            )
        solver.pop()
        if answer == z3.unknown:
            reason = str(solver.reason_unknown() or "").lower()
            if "timeout" in reason or "interrupt" in reason or "cancel" in reason:
                status, note = Status.TIMEOUT, (
                    f"Z3 timed out/was canceled after {config.timeout_ms} ms "
                    f"while deciding step {depth}; depths 0..{depth - 1} were "
                    "proven clean. A counterexample may exist at this depth or "
                    "beyond; increase timeout_ms or simplify the model.")
            else:
                why = reason or "no reason given"
                status, note = Status.UNKNOWN, (
                    f"Z3 returned UNKNOWN at step {depth} ({why}). "
                    f"Depths 0..{depth - 1} were proven clean; no conclusion "
                    "follows for this depth or beyond."
                )
            return CheckResult(
                status=status, contract=contract.name, bound=config.steps,
                depth=depth, violation=None,
                detail=str(solver.reason_unknown()), trace=[],
                elapsed_ms=elapsed, solver_calls=solver_calls, note=note,
            )
        # unsat -> extend the unrolling by one and continue

    return CheckResult(
        status=Status.SAFE_WITHIN_BOUND,
        contract=contract.name, bound=config.steps, depth=None,
        violation=None,
        detail=(f"No violating state is reachable within {config.steps} "
                "transition(s)."),
        trace=[],
        elapsed_ms=int((time.monotonic() - start) * 1000),
        solver_calls=solver_calls,
        note=("Bounded result only: executions of up to "
              f"{config.steps} step(s) were exhaustively analyzed. This is NOT "
              "a proof of safety for arbitrary depths; rerun with a larger "
              "bound to cover more."),
    )


def _add_transition_with(solver: Any, contract: Contract,
                         states: list[dict[str, Any]], k: int,
                         selectors: list[Any]) -> None:
    """Encode states[k] -> states[k+1] with gated per-action net deltas.

    For every action and every integer variable it assigns, a fresh
    **net-delta** symbol ``d`` is introduced with a linear domain::

        inactive action           -> d == 0
        active action             -> guard holds AND d == expr - old_value

    The next state is then the plain linear equation
    ``x[k+1] == x[k] + d_0 + d_1 + ...`` (and for bit-vectors, a widened-free
    per-action update). Booleans use a selector ITE. Putting the state
    equations *outside* the disjunction — instead of equating whole next
    states inside each branch — is what keeps Z3 linear: conservation then
    propagates by telescoping and the UNSAT query scales linearly in depth
    rather than exponentially. A missing debit simply means the active
    action's deltas do not sum to zero, so it is still detected.

    An integer selector (0..n-1) and inactive-parameter pinning keep the
    disjunction's free variables under control.
    """
    import z3

    n = len(contract.actions)
    selector = selectors[k]
    solver.add(selector >= 0, selector < n)
    indexed = sorted(contract.actions.items())

    # net delta of each variable contributed by each action
    int_deltas: dict[str, list[Any]] = {name: [] for name in contract.int_vars}
    bool_choices: dict[str, list[tuple[Any, Any]]] = {
        name: [] for name in contract.bool_vars
    }
    gate_disjuncts = []

    for i, (_, action) in enumerate(indexed):
        active = selector == i
        params = {p.name: _param_symbol(p, f"s{k}__a{i}__{p.name}")
                  for p in action.params}

        # parameter domains; parameters of an inactive action are pinned to
        # their lower bound (or false) so they cannot fuel case splits.
        for p in action.params:
            sym = params[p.name]
            if p.sort == "int" and p.width is None:
                if p.min is not None:
                    solver.add(sym >= p.min)
                if p.max is not None:
                    solver.add(sym <= p.max)
                if p.min is not None:
                    solver.add(z3.Implies(z3.Not(active), sym == p.min))
            elif p.sort == "int":
                if p.min is not None:
                    solver.add(z3.UGE(sym, p.min))
                if p.max is not None:
                    solver.add(z3.ULE(sym, p.max))
                if p.min is not None:
                    solver.add(z3.Implies(z3.Not(active),
                                          sym == z3.BitVecVal(p.min, p.width)))
            else:  # bool param
                solver.add(z3.Implies(z3.Not(active), sym == z3.BoolVal(False)))

        sym_table = dict(states[k])
        sym_table.update(params)
        for cname, cval in contract.constants.items():
            sym_table[cname] = z3.IntVal(cval)
        builder = SymbolicBuilder(_widths_table(contract, action))

        # build assigned terms; literals in a bv-target expression are
        # promoted to the target machine width.
        assigned = {}
        for target, expr in action.assign:
            tw = contract.variables[target].width
            assigned[target] = (
                SymbolicBuilder(_widths_table(contract, action), tw)
                .build(expr, sym_table) if tw is not None
                else builder.build(expr, sym_table)
            )
        guards = [builder.build(g, sym_table) for g in action.require]

        gate = z3.And(active, *guards)
        for name in contract.int_vars:
            var = contract.variables[name]
            if name in assigned:
                if var.width is None:
                    d = z3.Int(f"s{k}__a{i}__d__{name}")
                    # domain: keep deltas bounded via expr relation only;
                    # inactive -> 0, active -> expr - old
                    solver.add(z3.Implies(z3.Not(active), d == 0))
                    solver.add(z3.Implies(gate, d == assigned[name] - states[k][name]))
                    int_deltas[name].append(d)
                else:
                    w = var.width
                    d = z3.BitVec(f"s{k}__a{i}__d__{name}", w)
                    solver.add(z3.Implies(z3.Not(active),
                                          d == z3.BitVecVal(0, w)))
                    solver.add(z3.Implies(gate,
                                          d == assigned[name] - states[k][name]))
                    int_deltas[name].append(d)
            else:
                # unchanged variable contributes zero for this action
                zero = z3.IntVal(0) if var.width is None else z3.BitVecVal(0, var.width)
                int_deltas[name].append(zero)

        for name in contract.bool_vars:
            if name in assigned:
                bool_choices[name].append((active, assigned[name]))

        # at least the gate of the selected action must be consistent;
        # selector range already forces exactly one active action.
        gate_disjuncts.append(gate)

    # exactly one action is taken and its guard must hold
    solver.add(z3.Or(*gate_disjuncts))

    # linear(ish) next-state equations, outside the disjunction
    for name in contract.int_vars:
        total_delta = None
        for d in int_deltas[name]:
            total_delta = d if total_delta is None else total_delta + d
        solver.add(states[k + 1][name] == states[k][name] + total_delta)

    for name in contract.bool_vars:
        if bool_choices[name]:
            # default: unchanged; ITE over active actions
            value = states[k][name]
            for active, assigned_value in bool_choices[name]:
                value = z3.If(active, assigned_value, value)
            solver.add(states[k + 1][name] == value)
        # unassigned bool vars keep their value implicitly (no constraint),
        # but for a total next-state we pin it:
        if not bool_choices[name]:
            solver.add(states[k + 1][name] == states[k][name])

