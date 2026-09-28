"""Parsing of the JSON rule language into validated model objects.

Accepted request shape (see fixtures/ for examples)::

    {
      "domain": {
        "name": "resource-ops",
        "actions": [
          {
            "name": "move",
            "parameters": ["?w", "?from", "?to"],
            "preconditions": {"pos": ["at(?w,?from)", "clear(?to)"],
                              "neg": ["occupied(?to)"]},
            "add": ["at(?w,?to)", "clear(?from)"],
            "delete": ["at(?w,from)"],
            "cost": 1
          }
        ]
      },
      "problem": {
        "name": "demo",
        "objects": ["w1", "bench", "bin"],
        "init": ["at(w1,bench)", "clear(bin)"],
        "goal": {"pos": ["at(w1,bin)"], "neg": []}
      }
    }

Conventions:
* Variables are identifiers prefixed with ``?``; objects are plain
  identifiers matching ``[A-Za-z_][A-Za-z0-9_-]*``.
* A literal may be written as ``"pred(a,b)"`` / ``"~pred(a,b)"`` (the
  ``~`` or ``-`` prefix marks a negative literal) or as a JSON array
  ``["pred", "a", "b"]``.
* ``preconditions`` and ``goal`` accept either ``{"pos": [...], "neg": [...]}``
  or a flat list of signed literal strings.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import (
    ACTION_NAME_INVALID,
    ACTION_NAME_MISSING,
    DOMAIN_NAME_MISSING,
    GOAL_REQUIRED,
    LITERAL_MALFORMED,
    LITERAL_WRONG_TYPE,
    OBJECT_INVALID,
    OBJECT_REQUIRED,
    PARAM_INVALID,
    PARAM_MISSING,
    PROBLEM_NAME_MISSING,
    ValidationError,
)
from .model import ActionSchema, Atom, Domain, Problem

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_VAR_PREFIX = "?"

# Intermediate literal: atom tuple + negative flag.
RawLiteral = tuple[Atom, bool]


def is_variable(token: str) -> bool:
    return token.startswith(_VAR_PREFIX) and _NAME_RE.match(token[1:]) is not None


def is_name(token: str) -> bool:
    return bool(_NAME_RE.match(token))


def parse_domain(data: Any) -> Domain:
    if not isinstance(data, dict):
        raise ValidationError("domain must be an object", code=DOMAIN_NAME_MISSING)
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("domain.name is required", code=DOMAIN_NAME_MISSING)

    raw_actions = data.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ValidationError(
            "domain.actions must be a non-empty list", code=OBJECT_REQUIRED
        )

    schemas: list[ActionSchema] = []
    for index, raw in enumerate(raw_actions):
        schemas.append(_parse_action(raw, index))
    return Domain(name=name.strip(), actions=tuple(schemas))


def parse_problem(data: Any, domain: Domain) -> Problem:
    if not isinstance(data, dict):
        raise ValidationError("problem must be an object", code=PROBLEM_NAME_MISSING)
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("problem.name is required", code=PROBLEM_NAME_MISSING)

    raw_objects = data.get("objects", [])
    if not isinstance(raw_objects, list) or not raw_objects:
        raise ValidationError(
            "problem.objects must be a non-empty list", code=OBJECT_REQUIRED
        )
    objects: list[str] = []
    for raw in raw_objects:
        if not isinstance(raw, str) or not is_name(raw):
            raise ValidationError(
                f"invalid object {raw!r}: identifiers match [A-Za-z_][A-Za-z0-9_-]*",
                code=OBJECT_INVALID,
            )
        objects.append(raw)

    init = [
        _require_positive(_parse_literal(raw, f"problem.init[{i}]"))
        for i, raw in enumerate(data.get("init", []))
    ]
    pos, neg = _parse_literal_set(data.get("goal"), "problem.goal")
    if not pos and not neg:
        raise ValidationError("problem.goal must contain at least one literal",
                              code=GOAL_REQUIRED)

    return Problem(
        name=name.strip(),
        objects=tuple(objects),
        initial=frozenset(init),
        goal_pos=frozenset(pos),
        goal_neg=frozenset(neg),
    )


def _parse_action(raw: Any, index: int) -> ActionSchema:
    source = f"domain.actions[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{source} must be an object",
                              code=ACTION_NAME_MISSING)
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError(f"{source}.name is required",
                              code=ACTION_NAME_MISSING)
    if not is_name(name):
        raise ValidationError(
            f"{source}.name {name!r} is not a valid identifier",
            code=ACTION_NAME_INVALID,
        )

    raw_params = raw.get("parameters", [])
    if not isinstance(raw_params, list) or not raw_params:
        raise ValidationError(
            f"{source}.parameters must be a non-empty list", code=PARAM_MISSING
        )
    params: list[str] = []
    for raw_param in raw_params:
        if not isinstance(raw_param, str) or not is_variable(raw_param):
            raise ValidationError(
                f"{source} has invalid parameter {raw_param!r}; "
                "variables must look like '?x'",
                code=PARAM_INVALID,
            )
        params.append(raw_param)

    pre_pos, pre_neg = _parse_literal_set(
        raw.get("preconditions", []), f"{source}.preconditions"
    )
    add = [
        _require_positive(_parse_literal(raw_lit, f"{source}.add[{i}]"))
        for i, raw_lit in enumerate(raw.get("add", []))
    ]
    delete = [
        _require_positive(_parse_literal(raw_lit, f"{source}.delete[{i}]"))
        for i, raw_lit in enumerate(raw.get("delete", []))
    ]

    cost = raw.get("cost", 1)
    if not isinstance(cost, int) or isinstance(cost, bool) or cost <= 0:
        raise ValidationError(
            f"{source}.cost must be a positive integer", code="INVALID_COST"
        )

    return ActionSchema(
        name=name.strip(),
        parameters=tuple(params),
        pre_pos=frozenset(pre_pos),
        pre_neg=frozenset(pre_neg),
        add_effects=frozenset(add),
        del_effects=frozenset(delete),
        cost=cost,
    )


def _parse_literal_set(raw: Any, source: str) -> tuple[list[Atom], list[Atom]]:
    """Accept ``{'pos': [...], 'neg': [...]}`` or a signed literal list."""
    if isinstance(raw, dict):
        pos_raw = raw.get("pos", [])
        neg_raw = raw.get("neg", [])
        if not isinstance(pos_raw, list) or not isinstance(neg_raw, list):
            raise ValidationError(
                f"{source} keys 'pos' and 'neg' must be lists",
                code=LITERAL_WRONG_TYPE,
            )
        pos = [
            _require_positive(_parse_literal(item, f"{source}.pos[{i}]"))
            for i, item in enumerate(pos_raw)
        ]
        neg = [
            _require_positive(_parse_literal(item, f"{source}.neg[{i}]"))
            for i, item in enumerate(neg_raw)
        ]
        return pos, neg

    if isinstance(raw, list):
        pos: list[Atom] = []
        neg: list[Atom] = []
        for i, item in enumerate(raw):
            atom, is_neg = _parse_literal(item, f"{source}[{i}]")
            (neg if is_neg else pos).append(atom)
        return pos, neg

    raise ValidationError(
        f"{source} must be a list or {{'pos': [...], 'neg': [...]}}",
        code=LITERAL_WRONG_TYPE,
    )


def _parse_literal(raw: Any, source: str) -> RawLiteral:
    neg = False
    if isinstance(raw, list):
        parts = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if text[:1] in ("~", "-"):
            neg = True
            text = text[1:].strip()
        match = re.fullmatch(r"([^()\s]+)\s*(?:\(([^)]*)\))?", text)
        if not match:
            raise ValidationError(
                f"{source} is malformed: {raw!r}; expected 'pred(a,b)'",
                code=LITERAL_MALFORMED,
            )
        pred = match.group(1)
        args = [a.strip() for a in match.group(2).split(",")] if match.group(2) else []
        parts = [pred, *args]
    else:
        raise ValidationError(
            f"{source} must be a string or list, got {type(raw).__name__}",
            code=LITERAL_WRONG_TYPE,
        )

    if not parts or not all(isinstance(p, str) and p for p in parts):
        raise ValidationError(f"{source} is malformed: {raw!r}",
                              code=LITERAL_MALFORMED)
    atom: Atom = tuple(p.strip() for p in parts)  # type: ignore[assignment]
    return atom, neg


def _require_positive(parsed: RawLiteral) -> Atom:
    atom, neg = parsed
    if neg:
        raise ValidationError(
            f"negative literal not allowed here: {'~' + atom[0]}",
            code=LITERAL_MALFORMED,
        )
    return atom
