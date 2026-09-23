"""Loading and strict validation of explicit JSON contract models.

A contract is a finite-state machine specified declaratively as JSON::

    {
      "name": "safe-transfer",
      "description": "...",
      "state": {"int": {"alice": 10, "bob": 0}, "bool": {"locked": false}},
      "constants": {"FEE": 1},
      "actions": [
        {"name": "transfer",
         "params": [{"name": "amount", "type": "int",
                     "min": 0, "max": 100}],
         "require": [{"op": "not", "args": [{"var": "locked"}]}],
         "assign": [
           {"target": "alice", "expr": {"op": "-",
              "args": [{"var": "alice"}, {"var": "amount"}]}},
           {"target": "bob", "expr": {"op": "+",
              "args": [{"var": "bob"}, {"var": "amount"}]}}
         ]}
      ]
    }

Semantics are deliberately tiny and total:

* integer variables are mathematical integers by default; ``int_width`` on an
  int state variable or on an int parameter makes it a bit-vector with silent
  two's-complement wraparound (used to model EVM/uint overflow);
* an action is enabled iff every ``require`` expression evaluates true;
* enabled actions may be nondeterministically chosen at each step; the BMC
  explores all action/parameter choices symbolically;
* ``assign`` performs simultaneous (parallel) assignment using *pre-action*
  values; variables not listed keep their value. Booleans must be assigned
  bool-typed expressions and ints int-typed expressions.

No evaluation of arbitrary code ever happens: expressions are tagged JSON
objects built from a fixed operator whitelist (see :mod:`cbmc.terms`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ContractError, type_error
from .terms import int_signature, sort_expr

SUPPORTED_SCHEMA = "cbmc-contract/v1"


@dataclass(frozen=True)
class Var:
    name: str
    sort: str  # "int" | "bool"
    width: int | None = None  # set for bit-vector ints


@dataclass(frozen=True)
class Param:
    name: str
    sort: str
    min: int | None = None
    max: int | None = None
    width: int | None = None


@dataclass(frozen=True)
class Action:
    name: str
    params: tuple[Param, ...]
    require: tuple[dict[str, Any], ...]
    assign: tuple[tuple[str, dict[str, Any]], ...]
    target_sorts: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Contract:
    name: str
    description: str
    variables: dict[str, Var]
    constants: dict[str, int]
    actions: dict[str, Action]
    raw: dict[str, Any]
    initial_int: dict[str, int] = field(default_factory=dict)
    initial_bool: dict[str, bool] = field(default_factory=dict)

    @property
    def int_vars(self) -> list[str]:
        return sorted(n for n, v in self.variables.items() if v.sort == "int")

    @property
    def bool_vars(self) -> list[str]:
        return sorted(n for n, v in self.variables.items() if v.sort == "bool")


def _as_object(obj: Any, path: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise type_error("JSON object", obj, path)
    return obj


def _require_keys(obj: dict[str, Any], keys: tuple[str, ...], path: str) -> None:
    for key in keys:
        if key not in obj:
            raise ContractError(f"missing required key {key!r}", path)


def _parse_int_width(spec: Any, path: str) -> int | None:
    """Parse ``"int"`` (math int) or ``{"type": "int", "width": 256}`` (bv)."""
    if spec == "int":
        return None
    if isinstance(spec, dict):
        if spec.get("type") != "int":
            raise ContractError(f"unsupported type spec {spec!r}", path)
        width = spec.get("width")
        if not isinstance(width, int) or isinstance(width, bool) or width <= 0:
            raise ContractError("integer width must be a positive int", path)
        return width
    if spec == "bool":  # handled by callers; flag distinctly
        raise ContractError("expected integer type here, got 'bool'", path)
    raise ContractError(f"unsupported type spec {spec!r}", path)


def _parse_initial(obj: dict[str, Any]) -> tuple[
    dict[str, Var], dict[str, int], dict[str, bool]
]:
    state = _as_object(obj.get("state", {}), "state")
    variables: dict[str, Var] = {}
    initial_int: dict[str, int] = {}
    initial_bool: dict[str, bool] = {}

    ints = state.get("int", {})
    if not isinstance(ints, dict):
        raise type_error("object mapping name -> integer", ints, "state.int")
    for name, value in ints.items():
        if not isinstance(name, str) or not name:
            raise ContractError("state variable names must be non-empty strings",
                                "state.int")
        if not isinstance(value, int) or isinstance(value, bool):
            raise type_error("integer initial value", value, f"state.int.{name}")
        width = None
        if name in variables:
            raise ContractError(f"duplicate state variable {name!r}", "state")
        variables[name] = Var(name, "int", width)
        initial_int[name] = value

    # bit-vector (machine integer) variables declared via state.bv
    bvs = state.get("bv", {})
    if not isinstance(bvs, dict):
        raise type_error("object mapping name -> {width, init}", bvs, "state.bv")
    for name, spec in bvs.items():
        if name in variables:
            raise ContractError(f"duplicate state variable {name!r}", "state.bv")
        spec = _as_object(spec, f"state.bv.{name}")
        _require_keys(spec, ("width", "init"), f"state.bv.{name}")
        width = spec["width"]
        init = spec["init"]
        if not isinstance(width, int) or isinstance(width, bool) or not 1 <= width <= 4096:
            raise ContractError("width must be an int in [1, 4096]",
                                f"state.bv.{name}.width")
        if not isinstance(init, int) or isinstance(init, bool):
            raise type_error("integer initial value", init, f"state.bv.{name}.init")
        variables[name] = Var(name, "int", width)
        initial_int[name] = init

    bools = state.get("bool", {})
    if not isinstance(bools, dict):
        raise type_error("object mapping name -> boolean", bools, "state.bool")
    for name, value in bools.items():
        if name in variables:
            raise ContractError(f"duplicate state variable {name!r}", "state")
        if not isinstance(value, bool):
            raise type_error("boolean", value, f"state.bool.{name}")
        variables[name] = Var(name, "bool")
        initial_bool[name] = value

    return variables, initial_int, initial_bool


def _parse_params(raw: Any, action_path: str) -> tuple[Param, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise type_error("list of parameter specs", raw, f"{action_path}.params")
    params: list[Param] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        path = f"{action_path}.params[{i}]"
        item = _as_object(item, path)
        _require_keys(item, ("name", "type"), path)
        name = item["name"]
        if not isinstance(name, str) or not name:
            raise ContractError("parameter name must be a non-empty string", path)
        if name in seen:
            raise ContractError(f"duplicate parameter {name!r}", path)
        seen.add(name)
        type_spec = item["type"]
        if type_spec == "bool":
            params.append(Param(name, "bool"))
            continue
        width = _parse_int_width(type_spec, f"{path}.type")
        lo = item.get("min")
        hi = item.get("max")
        if lo is not None and (not isinstance(lo, int) or isinstance(lo, bool)):
            raise type_error("integer", lo, f"{path}.min")
        if hi is not None and (not isinstance(hi, int) or isinstance(hi, bool)):
            raise type_error("integer", hi, f"{path}.max")
        if lo is not None and hi is not None and lo > hi:
            raise ContractError(f"min {lo} > max {hi}", path)
        if width is not None:
            top = (1 << width) - 1
            if (lo is not None and lo < 0) or (hi is not None and hi < 0):
                raise ContractError(
                    f"unsigned {width}-bit parameter {name!r} needs non-negative bounds",
                    path,
                )
            if (lo is not None and lo > top) or (hi is not None and hi > top):
                raise ContractError(
                    f"bound for {name!r} exceeds unsigned {width}-bit range [0, {top}]",
                    path,
                )
        params.append(Param(name, "int", lo, hi, width))
    return tuple(params)


def _parse_action(raw: Any, index: int, variables: dict[str, Var],
                  constants: dict[str, int]) -> Action:
    path = f"actions[{index}]"
    raw = _as_object(raw, path)
    _require_keys(raw, ("name",), path)
    name = raw["name"]
    if not isinstance(name, str) or not name:
        raise ContractError("action name must be a non-empty string", path)

    params = _parse_params(raw.get("params"), path)
    scope = dict(variables)
    for p in params:
        if p.name in scope:
            raise ContractError(
                f"parameter {p.name!r} shadows a state variable", f"{path}.params"
            )
        scope[p.name] = Var(p.name, p.sort, p.width)
    for cname in constants:
        if cname not in scope:
            scope[cname] = Var(cname, "int")

    requires = raw.get("require", [])
    if not isinstance(requires, list):
        raise type_error("list of boolean expressions", requires, f"{path}.require")
    for i, expr in enumerate(requires):
        expr = _as_object(expr, f"{path}.require[{i}]")
        sort_expr(expr, scope, f"{path}.require[{i}]", expected="bool")

    assigns = raw.get("assign", [])
    if not isinstance(assigns, list):
        raise type_error("list of assignments", assigns, f"{path}.assign")
    parsed_assign: list[tuple[str, dict[str, Any]]] = []
    targets: set[str] = set()
    target_sorts: dict[str, str] = {}
    for i, item in enumerate(assigns):
        apath = f"{path}.assign[{i}]"
        item = _as_object(item, apath)
        _require_keys(item, ("target", "expr"), apath)
        target = item["target"]
        if target not in variables:
            raise ContractError(
                f"assignment target {target!r} is not a declared state variable",
                apath,
            )
        if target in targets:
            raise ContractError(
                f"target {target!r} assigned twice in one action "
                "(parallel assignment needs a single target)",
                apath,
            )
        targets.add(target)
        expr = _as_object(item["expr"], f"{apath}.expr")
        wanted = variables[target].sort
        sort_expr(expr, scope, f"{apath}.expr", expected=wanted)
        tgt_width = variables[target].width
        if wanted == "int":
            has_math, expr_widths = int_signature(expr, scope, f"{apath}.expr")
            if tgt_width is None and expr_widths:
                raise ContractError(
                    f"cannot assign bit-vector expression to math-int "
                    f"{target!r}", apath,
                )
            if tgt_width is not None and has_math:
                raise ContractError(
                    f"cannot assign math-integer expression to {tgt_width}-bit "
                    f"machine integer {target!r}", apath,
                )
            if tgt_width is not None and tgt_width not in expr_widths \
                    and expr_widths:
                raise ContractError(
                    f"bit-vector width mismatch assigning to {target!r} "
                    f"(target {tgt_width}, expression "
                    f"{sorted(expr_widths)})", apath,
                )
        target_sorts[target] = wanted
        parsed_assign.append((target, expr))

    return Action(name, params, tuple(requires), tuple(parsed_assign),
                  target_sorts)


def load_contract(data: Any) -> Contract:
    """Validate a JSON-decoded contract and return the parsed :class:`Contract`."""
    obj = _as_object(data, "$")
    schema = obj.get("schema", SUPPORTED_SCHEMA)
    if schema != SUPPORTED_SCHEMA:
        raise ContractError(
            f"unsupported schema {schema!r}; expected {SUPPORTED_SCHEMA!r}", "$"
        )
    name = obj.get("name", "unnamed")
    if not isinstance(name, str):
        raise type_error("string", name, "name")
    description = obj.get("description", "")
    if not isinstance(description, str):
        raise type_error("string", description, "description")

    constants_raw = obj.get("constants", {})
    if not isinstance(constants_raw, dict):
        raise type_error("object mapping name -> integer", constants_raw,
                         "constants")
    constants: dict[str, int] = {}
    for cname, cval in constants_raw.items():
        if not isinstance(cname, str) or not cname:
            raise ContractError("constant names must be non-empty strings",
                                "constants")
        if not isinstance(cval, int) or isinstance(cval, bool):
            raise type_error("integer", cval, f"constants.{cname}")
        constants[cname] = cval

    variables, initial_int, initial_bool = _parse_initial(obj)

    actions_raw = obj.get("actions", [])
    if not isinstance(actions_raw, list) or not actions_raw:
        raise ContractError("actions must be a non-empty list", "actions")
    names: set[str] = set()
    actions: dict[str, Action] = {}
    for i, raw_action in enumerate(actions_raw):
        action = _parse_action(raw_action, i, variables, constants)
        if action.name in names:
            raise ContractError(f"duplicate action name {action.name!r}",
                                f"actions[{i}]")
        names.add(action.name)
        actions[action.name] = action

    return Contract(
        name=name,
        description=description,
        variables=variables,
        constants=constants,
        actions=actions,
        raw=obj,
        initial_int=initial_int,
        initial_bool=initial_bool,
    )


def load_contract_file(path: str | Path) -> Contract:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON in {path}: {exc}") from exc
    return load_contract(data)
