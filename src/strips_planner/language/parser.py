"""Parser for the STRIPS rule language.

The accepted problem format is JSON with S-expression atoms. Two atom spellings
are accepted everywhere::

    "(at robot site1)"
    ["at", "robot", "site1"]

Problem outline::

    {
      "name": "resource-ops",
      "types": ["location", "robot"],
      "objects": {"location": ["depot", "site"], "robot": ["r1"]},
      "predicates": {"at":  {"types": ["robot", "location"], "static": false}},
      "actions": [
        {
          "name": "move",
          "parameters": [{"name": "?r", "type": "robot"}, ...],
          "cost": 1,
          "preconditions": {"pos": ["(at ?r ?from)"], "neg": ["(blocked ?to)"]},
          "effects": {"add": ["(at ?r ?to)"], "del": ["(at ?r ?from)"]}
        }
      ],
      "init": ["(at r1 depot)"],
      "goal": {"pos": ["(at r1 site)"], "neg": []}
    }

The parser only enforces *shape* (syntactic) rules and produces raw structures.
Semantic rules (unknown references, typing, grounding) live in
:mod:`strips_planner.language.validator`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from strips_planner.errors import IssueCode, ProblemParseError


@dataclass(frozen=True, slots=True)
class RawAtom:
    predicate: str
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RawParameter:
    name: str
    type_name: str


@dataclass(frozen=True, slots=True)
class RawSchema:
    name: str
    parameters: tuple[RawParameter, ...]
    pre_pos: tuple[RawAtom, ...]
    pre_neg: tuple[RawAtom, ...]
    add: tuple[RawAtom, ...]
    delete: tuple[RawAtom, ...]
    cost: float


@dataclass(frozen=True, slots=True)
class RawProblem:
    name: str
    types: tuple[str, ...]
    objects: dict[str, tuple[str, ...]]
    predicates: dict[str, dict[str, Any]]
    actions: tuple[RawSchema, ...]
    init: tuple[RawAtom, ...]
    goal_pos: tuple[RawAtom, ...]
    goal_neg: tuple[RawAtom, ...]
    notes: tuple[str, ...] = field(default=())


_TOP_FIELDS = {"name", "types", "objects", "predicates", "actions", "init", "goal", "notes"}
_SCHEMA_FIELDS = {"name", "parameters", "preconditions", "effects", "cost"}
_PARAM_FIELDS = {"name", "type"}
_PRED_FIELDS = {"types", "static"}
_GOAL_FIELDS = {"pos", "neg", "positive", "negative"}
_PRE_FIELDS = {"pos", "neg", "positive", "negative"}
_EFF_FIELDS = {"add", "del", "delete"}


def parse_json(text: str) -> RawProblem:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProblemParseError(
            f"request body is not valid JSON: {exc.msg}",
            code=IssueCode.SYNTAX_ERROR,
            details={"line": exc.lineno, "column": exc.colno},
        ) from exc
    return parse_dict(data)


def parse_dict(data: Any) -> RawProblem:
    if not isinstance(data, dict):
        raise ProblemParseError(
            "problem payload must be a JSON object", code=IssueCode.NOT_AN_OBJECT
        )
    _reject_extra_fields(data, _TOP_FIELDS, "$", IssueCode.UNKNOWN_FIELD)

    name = _require_str(data, "name", "$")
    types = tuple(_require_str_list(data.get("types", []), "$.types"))
    objects = _parse_objects(data.get("objects", {}))
    predicates = _parse_predicate_decls(data.get("predicates", {}))
    actions = tuple(
        _parse_schema(item, f"$.actions[{i}]")
        for i, item in enumerate(_require_list(data.get("actions", []), "$.actions"))
    )
    init = tuple(_parse_atoms(data.get("init", []), "$.init"))
    gpos, gneg = _parse_goal(data.get("goal", {}))
    notes_raw = data.get("notes", [])
    notes = tuple(_require_str_list(notes_raw, "$.notes")) if notes_raw else ()

    return RawProblem(
        name=name,
        types=types,
        objects=objects,
        predicates=predicates,
        actions=actions,
        init=init,
        goal_pos=gpos,
        goal_neg=gneg,
        notes=notes,
    )


def _parse_goal(value: Any) -> tuple[tuple[RawAtom, ...], tuple[RawAtom, ...]]:
    if not isinstance(value, dict):
        raise ProblemParseError("goal must be an object", code=IssueCode.NOT_AN_OBJECT)
    _reject_extra_fields(value, _GOAL_FIELDS, "$.goal", IssueCode.UNKNOWN_FIELD)
    pos = value.get("pos", value.get("positive", []))
    neg = value.get("neg", value.get("negative", []))
    return tuple(_parse_atoms(pos, "$.goal.pos")), tuple(_parse_atoms(neg, "$.goal.neg"))


def _parse_objects(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise ProblemParseError(
            "objects must map type names to name lists", code=IssueCode.NOT_AN_OBJECT
        )
    return {key: tuple(_require_str_list(items, f"$.objects.{key}"))
            for key, items in value.items()}


def _parse_predicate_decls(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ProblemParseError(
            "predicates must map predicate names to declarations",
            code=IssueCode.NOT_AN_OBJECT,
        )
    out: dict[str, dict[str, Any]] = {}
    for pname, decl in value.items():
        _require_name(pname, "predicate name", "$.predicates")
        if not isinstance(decl, dict):
            raise ProblemParseError(
                f"predicate {pname!r} declaration must be an object",
                code=IssueCode.NOT_AN_OBJECT,
            )
        _reject_extra_fields(decl, _PRED_FIELDS, f"$.predicates.{pname}",
                             IssueCode.UNKNOWN_FIELD)
        types = tuple(_require_str_list(decl.get("types", []),
                                        f"$.predicates.{pname}.types"))
        static = decl.get("static", False)
        if not isinstance(static, bool):
            raise ProblemParseError(
                f"predicate {pname!r} static flag must be true or false",
                code=IssueCode.NOT_A_STRING,
            )
        out[pname] = {"types": types, "static": static}
    return out


def _parse_schema(value: Any, location: str) -> RawSchema:
    if not isinstance(value, dict):
        raise ProblemParseError(f"{location} must be an object",
                                code=IssueCode.NOT_AN_OBJECT)
    _reject_extra_fields(value, _SCHEMA_FIELDS, location, IssueCode.UNKNOWN_FIELD)

    name = _require_str(value, "name", location)
    _require_name(name, "action name", location)

    params_raw = _require_list(value.get("parameters", []), f"{location}.parameters")
    parameters: list[RawParameter] = []
    for i, p in enumerate(params_raw):
        ploc = f"{location}.parameters[{i}]"
        if not isinstance(p, dict):
            raise ProblemParseError(f"{ploc} must be an object",
                                    code=IssueCode.NOT_AN_OBJECT)
        _reject_extra_fields(p, _PARAM_FIELDS, ploc, IssueCode.UNKNOWN_FIELD)
        pname = _require_str(p, "name", ploc)
        if not pname.startswith("?") or len(pname) == 1:
            raise ProblemParseError(
                f"{ploc}.name must be a parameter starting with '?' (got {pname!r})",
                code=IssueCode.SYNTAX_ERROR,
            )
        ptype = _require_str(p, "type", ploc)
        parameters.append(RawParameter(pname, ptype))

    pre = value.get("preconditions", {})
    eff = value.get("effects", {})
    pre_block = _parse_literal_block(pre, f"{location}.preconditions", _PRE_FIELDS)
    eff_block = _parse_literal_block(eff, f"{location}.effects", _EFF_FIELDS,
                                     neg_key=None)
    cost = _parse_cost(value.get("cost", 1.0), location)

    return RawSchema(
        name=name,
        parameters=tuple(parameters),
        pre_pos=pre_block[0],
        pre_neg=pre_block[1],
        add=eff_block[0],
        delete=eff_block[1],
        cost=cost,
    )


def _parse_literal_block(
    value: Any,
    location: str,
    allowed: set[str],
    *,
    neg_key: str | None = "neg",
) -> tuple[tuple[RawAtom, ...], tuple[RawAtom, ...]]:
    if not isinstance(value, dict):
        raise ProblemParseError(f"{location} must be an object",
                                code=IssueCode.NOT_AN_OBJECT)
    _reject_extra_fields(value, allowed, location, IssueCode.UNKNOWN_FIELD)
    if "add" in allowed:
        # Effects block: positive slot is "add", negative slot is "del"/"delete".
        del_key = "del" if "del" in value else "delete"
        pos = tuple(_parse_atoms(value.get("add", []), f"{location}.add"))
        neg = tuple(_parse_atoms(value.get(del_key, []), f"{location}.{del_key}"))
        return pos, neg
    # Precondition/goal block: "pos"/"positive" and "neg"/"negative".
    pos_key = "pos" if "pos" in value else "positive"
    effective_neg_key = neg_key if neg_key in value else "negative"
    pos = tuple(_parse_atoms(value.get(pos_key, []), f"{location}.{pos_key}"))
    neg = tuple(_parse_atoms(value.get(effective_neg_key, []),
                             f"{location}.{effective_neg_key}"))
    return pos, neg


def _parse_cost(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProblemParseError(
            f"{location}.cost must be a positive number",
            code=IssueCode.INVALID_COST,
        )
    cost = float(value)
    if cost <= 0 or cost != cost or cost == float("inf"):
        raise ProblemParseError(
            f"{location}.cost must be a finite positive number",
            code=IssueCode.INVALID_COST,
        )
    return cost


def _parse_atoms(value: Any, location: str) -> list[RawAtom]:
    items = _require_list(value, location)
    return [_parse_atom(item, f"{location}[{i}]") for i, item in enumerate(items)]


def _parse_atom(value: Any, location: str) -> RawAtom:
    if isinstance(value, str):
        tokens = _tokenize_sexpr(value, location)
    elif isinstance(value, list):
        if not value or not all(isinstance(t, str) for t in value):
            raise ProblemParseError(
                f"{location} atom must be a non-empty list of strings",
                code=IssueCode.SYNTAX_ERROR,
            )
        tokens = value
    else:
        raise ProblemParseError(
            f"{location} atom must be a string or list of strings",
            code=IssueCode.NOT_A_STRING,
        )
    predicate, args = tokens[0], tuple(tokens[1:])
    _require_name(predicate, "predicate", location)
    return RawAtom(predicate, args)


def _tokenize_sexpr(text: str, location: str) -> list[str]:
    stripped = text.strip()
    if stripped.startswith("(") != stripped.endswith(")") or not stripped.startswith("("):
        raise ProblemParseError(
            f"{location} atom {text!r} must look like '(predicate arg ...)'",
            code=IssueCode.SYNTAX_ERROR,
        )
    inner = stripped[1:-1]
    if "(" in inner or ")" in inner:
        raise ProblemParseError(
            f"{location} nested lists are not allowed inside atom {text!r}",
            code=IssueCode.SYNTAX_ERROR,
        )
    tokens = inner.split()
    if not tokens:
        raise ProblemParseError(f"{location} atom is empty",
                                code=IssueCode.SYNTAX_ERROR)
    return tokens


def _require_str(data: dict[str, Any], key: str, location: str) -> str:
    if key not in data:
        raise ProblemParseError(f"{location} is missing field {key!r}",
                                code=IssueCode.MISSING_FIELD)
    val = data[key]
    if not isinstance(val, str) or not val.strip():
        raise ProblemParseError(f"{location}.{key} must be a non-empty string",
                                code=IssueCode.NOT_A_STRING)
    return val


def _require_str_list(value: Any, location: str) -> list[str]:
    items = _require_list(value, location)
    out: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            raise ProblemParseError(f"{location}[{i}] must be a non-empty string",
                                    code=IssueCode.NOT_A_STRING)
        out.append(item)
    return out


def _require_list(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProblemParseError(f"{location} must be a list",
                                code=IssueCode.NOT_A_LIST)
    return value


def _require_name(name: str, what: str, location: str) -> None:
    if not name or any(c.isspace() for c in name) or name.startswith("?"):
        raise ProblemParseError(
            f"{location}: invalid {what} {name!r} (no whitespace, parameters only in action args)",
            code=IssueCode.SYNTAX_ERROR,
        )


def _reject_extra_fields(
    data: dict[str, Any], allowed: set[str], location: str, code: str
) -> None:
    for key in data:
        if key not in allowed:
            raise ProblemParseError(
                f"{location} has unknown field {key!r}; allowed: {sorted(allowed)}",
                code=code,
            )
