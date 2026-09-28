"""Parsers for rule documents.

Two front-ends share one validator, so the behaviour is identical:

* :func:`parse_rules_json` - JSON document (API / fixtures)
* :func:`parse_rules`      - tiny text DSL (human-friendly examples/tests)

DSL example::

    rule "same-department pair" salience 10
    on Employee(dept = ?d, salary > 50000, unique_fields=["id"])
    on Employee(dept = ?d, id != ?eid)
    action:
      assert Pair(a = ?eid, b = ?eid2)
      retract 0
      stop
    end
"""

from __future__ import annotations

import json
import re
from typing import Any

from .model import (
    Action,
    AssertTemplate,
    Binding,
    ConditionalElement,
    LiteralConstraint,
    Operator,
    Rule,
    VariableConstraint,
)

_VARIABLE_RE = re.compile(r"^\?[A-Za-z_][A-Za-z0-9_]*$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")


class RuleParseError(ValueError):
    """Raised for any malformed rule document (syntax or static semantics)."""


# ---------------------------------------------------------------------------
# JSON front-end
# ---------------------------------------------------------------------------

def parse_rules_json(document: str | dict[str, Any] | list[dict[str, Any]]) -> list[Rule]:
    if isinstance(document, str):
        try:
            raw = json.loads(document)
        except json.JSONDecodeError as exc:
            raise RuleParseError(f"invalid JSON rule document: {exc}") from exc
    else:
        raw = document
    if isinstance(raw, dict) and "rules" in raw:
        raw = raw["rules"]
    if not isinstance(raw, list):
        raise RuleParseError("rule document must be a list of rules or {'rules': [...]}")
    return [_rule_from_dict(item, index) for index, item in enumerate(raw)]


def _rule_from_dict(data: dict[str, Any], index: int) -> Rule:
    if not isinstance(data, dict):
        raise RuleParseError(f"rule #{index} must be an object")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuleParseError(f"rule #{index} requires a non-empty string 'name'")
    conditions_raw = data.get("conditions")
    if not isinstance(conditions_raw, list) or not conditions_raw:
        raise RuleParseError(f"rule {name!r}: 'conditions' must be a non-empty list")
    conditions = tuple(_ce_from_dict(c, name, i) for i, c in enumerate(conditions_raw))
    action = _action_from_dict(data.get("action", {}), name, len(conditions))
    salience = data.get("salience", 0)
    if not isinstance(salience, int) or isinstance(salience, bool):
        raise RuleParseError(f"rule {name!r}: 'salience' must be an integer")
    enabled = data.get("enabled", True)
    refraction = data.get("refraction", True)
    if not isinstance(enabled, bool) or not isinstance(refraction, bool):
        raise RuleParseError(f"rule {name!r}: 'enabled'/'refraction' must be booleans")
    return Rule(
        name=name,
        conditions=conditions,
        action=action,
        salience=salience,
        enabled=enabled,
        refraction=refraction,
    )


def _ce_from_dict(data: Any, rule_name: str, ce_index: int) -> ConditionalElement:
    if not isinstance(data, dict):
        raise RuleParseError(f"rule {rule_name!r}: condition #{ce_index} must be an object")
    ce_type = data.get("type")
    if not isinstance(ce_type, str) or not ce_type:
        raise RuleParseError(f"rule {rule_name!r}: condition #{ce_index} needs string 'type'")
    constraints = tuple(
        _constraint_from_dict(c, rule_name, ce_index) for c in data.get("constraints", [])
    )
    return ConditionalElement(type=ce_type, constraints=constraints)


def _constraint_from_dict(
    data: Any, rule_name: str, ce_index: int
) -> LiteralConstraint | VariableConstraint | Binding:
    if not isinstance(data, dict):
        raise RuleParseError(
            f"rule {rule_name!r} condition #{ce_index}: constraint must be an object"
        )
    field = data.get("field")
    if not isinstance(field, str) or not field:
        raise RuleParseError(
            f"rule {rule_name!r} condition #{ce_index}: constraint needs string 'field'"
        )
    if "variable" in data and "op" not in data:
        # Shorthand: {"field": "dept", "variable": "d"} == equality binding.
        return _make_binding_or_var(field, "==", data["variable"], rule_name, ce_index)
    op = Operator.from_raw(str(data.get("op", "==")))
    if "value" in data:
        return LiteralConstraint(field=field, op=op, value=_json_literal(data["value"], field))
    if "variable" in data:
        return _make_binding_or_var(field, op.value, data["variable"], rule_name, ce_index)
    raise RuleParseError(
        f"rule {rule_name!r} condition #{ce_index}: constraint {field!r} needs 'value' or 'variable'"
    )


def _make_binding_or_var(
    field: str, op_raw: str, variable_raw: Any, rule_name: str, ce_index: int
):
    if not isinstance(variable_raw, str) or not _VARIABLE_RE.match(variable_raw):
        raise RuleParseError(
            f"rule {rule_name!r} condition #{ce_index}: variable must look like ?name, "
            f"got {variable_raw!r}"
        )
    variable = variable_raw[1:]
    op = Operator.from_raw(op_raw)
    if op is Operator.EQ:
        return Binding(field=field, variable=variable)
    return VariableConstraint(field=field, op=op, variable=variable)


def _json_literal(value: Any, field: str) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        if not all(isinstance(v, (str, int, float, bool)) for v in value):
            raise RuleParseError(f"constraint on {field!r}: list literals must hold primitives")
        return value
    raise RuleParseError(f"constraint on {field!r}: unsupported literal {type(value).__name__}")


def _action_from_dict(data: Any, rule_name: str, n_conditions: int) -> Action:
    if not isinstance(data, dict):
        raise RuleParseError(f"rule {rule_name!r}: action must be an object")
    asserts = []
    for item in data.get("assert", []):
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise RuleParseError(f"rule {rule_name!r}: action.assert entries need a 'type'")
        fields = item.get("fields", {})
        if not isinstance(fields, dict):
            raise RuleParseError(f"rule {rule_name!r}: action assert fields must be an object")
        asserts.append(AssertTemplate(type=item["type"], fields=dict(fields)))
    retract = data.get("retract_ce_indices", [])
    if not isinstance(retract, list) or not all(isinstance(i, int) for i in retract):
        raise RuleParseError(f"rule {rule_name!r}: retract_ce_indices must be ints")
    if any(i < 0 or i >= n_conditions for i in retract):
        raise RuleParseError(f"rule {rule_name!r}: retract index out of range 0..{n_conditions - 1}")
    stop = data.get("stop", False)
    if not isinstance(stop, bool):
        raise RuleParseError(f"rule {rule_name!r}: stop must be boolean")
    return Action(
        asserts=tuple(asserts),
        retract_ce_indices=tuple(retract),
        stop=stop,
    )


# ---------------------------------------------------------------------------
# Tiny text DSL
# ---------------------------------------------------------------------------

_RULE_HEAD_RE = re.compile(
    r'rule\s+"(?P<name>[^"]+)"(?:\s+salience\s+(?P<salience>-?\d+))?\s*$'
)
_CE_RE = re.compile(r"^on\s+(?P<type>[A-Za-z_][\w.\-]*)\((?P<body>.*)\)\s*$")
_ASSERT_RE = re.compile(r"^\s*assert\s+(?P<type>[A-Za-z_][\w.\-]*)\((?P<body>.*)\)\s*$")
_RETRACT_RE = re.compile(r"^\s*retract\s+(?P<idx>\d+(?:\s*,\s*\d+)*)\s*$")


def parse_rules(text: str) -> list[Rule]:
    """Parse the text DSL. Each rule starts with ``rule`` and ends with ``end``."""

    rules: list[Rule] = []
    blocks = [b for b in (block.strip() for block in text.split("end")) if b.strip()]
    for block in blocks:
        rules.append(_parse_rule_block(block))
    if not rules:
        raise RuleParseError("no rules found in DSL text")
    return rules


def _parse_rule_block(block: str) -> Rule:
    lines = [ln.strip() for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise RuleParseError("empty rule block")
    head = _RULE_HEAD_RE.match(lines[0])
    if not head:
        raise RuleParseError(f"bad rule header: {lines[0]!r}")
    name = head.group("name")
    salience = int(head.group("salience")) if head.group("salience") else 0

    conditions: list[ConditionalElement] = []
    asserts: list[AssertTemplate] = []
    retract: list[int] = []
    stop = False
    in_action = False
    for raw in lines[1:]:
        low = raw.lower()
        if low == "action:":
            in_action = True
            continue
        if not in_action:
            match = _CE_RE.match(raw)
            if not match:
                raise RuleParseError(f"rule {name!r}: expected 'on Type(...)', got {raw!r}")
            conditions.append(_parse_ce(match.group("type"), match.group("body"), name))
        else:
            if low == "stop":
                stop = True
                continue
            am = _ASSERT_RE.match(raw)
            if am:
                asserts.append(_parse_assert(am.group("type"), am.group("body"), name))
                continue
            rm = _RETRACT_RE.match(raw)
            if rm:
                retract.extend(int(x) for x in re.findall(r"\d+", rm.group("idx")))
                continue
            raise RuleParseError(f"rule {name!r}: unknown action line {raw!r}")
    rule_dict = {
        "name": name,
        "salience": salience,
        "conditions": [_ce_to_serializable(c) for c in conditions],
        "action": {
            "assert": [{"type": a.type, "fields": a.fields} for a in asserts],
            "retract_ce_indices": retract,
            "stop": stop,
        },
    }
    return _rule_from_dict(rule_dict, 0)


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = []
    for ch in body:
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_ce(ce_type: str, body: str, rule_name: str) -> ConditionalElement:
    constraints: list[dict[str, Any]] = []
    for part in _split_top_level(body):
        constraints.append(_parse_constraint_expr(part, rule_name))
    return ConditionalElement(
        type=ce_type,
        constraints=tuple(
            _constraint_from_dict(c, rule_name, i) for i, c in enumerate(constraints)
        ),
    )


def _parse_constraint_expr(expr: str, rule_name: str) -> dict[str, Any]:
    for op_raw in ("not_in", "in", "!=", "<=", ">=", "==", "<", ">", "="):
        marker = f" {op_raw} " if op_raw in ("in", "not_in") else op_raw
        if marker in expr:
            field, raw_value = (s.strip() for s in expr.split(marker, 1))
            normalized_op = "==" if op_raw == "=" else op_raw
            if _VARIABLE_RE.match(raw_value):
                return {"field": field, "op": normalized_op, "variable": raw_value}
            return {"field": field, "op": normalized_op, "value": json.loads(raw_value)}
    if _IDENT_RE.match(expr):
        # Bare ``field`` shorthand -> bind field value to ?field.
        return {"field": expr, "variable": "?" + expr}
    raise RuleParseError(f"rule {rule_name!r}: cannot parse constraint {expr!r}")


def _parse_assert(assert_type: str, body: str, rule_name: str) -> AssertTemplate:
    fields: dict[str, Any] = {}
    for part in _split_top_level(body):
        if "=" not in part:
            raise RuleParseError(f"rule {rule_name!r}: assert field needs '=' in {part!r}")
        fname, raw = (s.strip() for s in part.split("=", 1))
        if _VARIABLE_RE.match(raw):
            fields[fname] = {"variable": raw[1:]}
        else:
            fields[fname] = {"value": json.loads(raw)}
    return AssertTemplate(type=assert_type, fields=fields)


def _ce_to_serializable(ce: ConditionalElement) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for c in ce.constraints:
        if isinstance(c, Binding):
            out.append({"field": c.field, "variable": "?" + c.variable})
        elif isinstance(c, VariableConstraint):
            out.append({"field": c.field, "op": c.op.value, "variable": "?" + c.variable})
        else:
            out.append({"field": c.field, "op": c.op.value, "value": c.value})
    return {"type": ce.type, "constraints": out}
