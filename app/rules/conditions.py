"""Fact-based state and the condition/effect mini language.

A *state* maps fact names to integer or boolean values. Missing facts
read as ``0`` (closed-world numeric default) so action preconditions can
demand a fact be present without every problem having to initialise it.

Conditions (used for start preconditions, duration invariants and goals)
are plain JSON/YAML structures:

* ``{"fact": "energy", "op": ">=", "value": 2}``
* ``{"all": [cond, ...]}`` / ``{"any": [...]}`` / ``{"not": cond}``
* ``{"always": true}`` -- vacuous condition

Effects (applied at an action's end instant) use the same comparison
table but as assignment operators:

* ``{"fact": "energy", "op": "=", "value": 0}``
* ``{"fact": "energy", "op": "-=", "value": 1}``  (also ``"+="``)

Effects on boolean facts with ``"="`` keep booleans; arithmetic ops are
rejected at validation time for booleans.
"""
from __future__ import annotations

from typing import Union

from .errors import ValidationFailure

StateValue = Union[int, bool]
State = dict[str, StateValue]

COMPARISONS = {"==", "!=", ">=", "<=", ">", "<"}
ASSIGNMENTS = {"=", "+=", "-="}


def normalize_state(raw: object, *, field: str) -> State:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationFailure(f"{field} must be a mapping of fact -> value", got=type(raw).__name__)
    state: State = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise ValidationFailure(f"{field} fact names must be non-empty strings", got=repr(key))
    for key, value in raw.items():
        if isinstance(value, bool):
            state[key] = value
        elif isinstance(value, int):
            state[key] = value
        else:
            raise ValidationFailure(
                f"{field}.{key} must be int or bool", got=repr(value), got_type=type(value).__name__
            )
    return state


def read(state: State, fact: str) -> StateValue:
    """Closed-world read: an unset fact is ``0``."""
    return state.get(fact, 0)


def _compare(actual: StateValue, op: str, expected: object) -> bool:
    if op not in COMPARISONS:
        raise ValidationFailure("unknown comparison operator", op=op, allowed=sorted(COMPARISONS))
    if op in {"==", "!="}:
        result = actual == expected
        return result if op == "==" else not result
    if isinstance(actual, bool) or isinstance(expected, bool):
        raise ValidationFailure(
            "order comparisons require integer operands on both sides",
            op=op,
            actual=actual,
            expected=expected,
        )
    if not isinstance(expected, int):
        raise ValidationFailure("comparison value must be int for order tests", op=op, got=repr(expected))
    if op == ">=":
        return actual >= expected
    if op == "<=":
        return actual <= expected
    if op == ">":
        return actual > expected
    return actual < expected


def evaluate(condition: object, state: State) -> bool:
    """Evaluate a condition structure against ``state``.

    Validated structures are expected; malformed input raises
    ``ValidationFailure`` rather than silently evaluating false, because a
    typo in a rule must never masquerade as a satisfied/unsat condition.
    """
    if condition is None:
        return True
    if not isinstance(condition, dict) or len(condition) != 1:
        raise ValidationFailure(
            "condition must be a single-key mapping",
            got=condition,
            forms=sorted(["fact", "all", "any", "not", "always"]),
        )
    (key, body), = condition.items()
    if key == "always":
        if body is not True:
            raise ValidationFailure("'always' condition only accepts true", got=body)
        return True
    if key == "all":
        return all(evaluate(c, state) for c in _as_list(body, "all"))
    if key == "any":
        return any(evaluate(c, state) for c in _as_list(body, "any"))
    if key == "not":
        return not evaluate(body, state)
    if key == "fact":
        if not isinstance(body, dict):
            raise ValidationFailure("'fact' condition requires a mapping", got=body)
        fact = body.get("fact")
        op = body.get("op", "==")
        if "value" not in body:
            raise ValidationFailure("'fact' condition requires 'value'", got=body)
        if not isinstance(fact, str) or not fact:
            raise ValidationFailure("'fact' must name a non-empty fact", got=fact)
        return _compare(read(state, fact), op, body["value"])
    raise ValidationFailure("unknown condition form", form=key)


def _as_list(body: object, form: str) -> list[object]:
    if not isinstance(body, list):
        raise ValidationFailure(f"'{form}' requires a list", got=type(body).__name__)
    return body


def apply_effects(effects: object, state: State) -> State:
    """Return a **new** state with ``effects`` applied (never mutates input)."""
    if effects is None:
        return dict(state)
    if not isinstance(effects, list):
        raise ValidationFailure("effects must be a list", got=type(effects).__name__)
    next_state: State = dict(state)
    for raw in effects:
        if not isinstance(raw, dict):
            raise ValidationFailure("each effect must be a mapping", got=raw)
        fact = raw.get("fact")
        op = raw.get("op", "=")
        if "value" not in raw:
            raise ValidationFailure("effect requires 'value'", got=raw)
        value = raw["value"]
        if not isinstance(fact, str) or not fact:
            raise ValidationFailure("effect 'fact' must be a non-empty string", got=fact)
        if op not in ASSIGNMENTS:
            raise ValidationFailure("unknown effect operator", op=op, allowed=sorted(ASSIGNMENTS))
        current = read(next_state, fact)
        if op == "=":
            if not isinstance(value, (int, bool)) or isinstance(value, float):
                raise ValidationFailure("assignment value must be int or bool", got=repr(value))
            next_state[fact] = value
            continue
        if isinstance(current, bool) or isinstance(value, bool):
            raise ValidationFailure(
                "arithmetic effects require integer operands",
                fact=fact,
                op=op,
                current=current,
                value=value,
            )
        if not isinstance(value, int):
            raise ValidationFailure("arithmetic effect value must be int", got=repr(value))
        next_state[fact] = current + value if op == "+=" else current - value
    return next_state


def validate_condition(condition: object, *, path: str = "condition") -> None:
    """Structural validation used while parsing problem definitions."""
    if condition is None:
        return
    if not isinstance(condition, dict) or len(condition) != 1:
        raise ValidationFailure(f"{path}: condition must be a single-key mapping", got=condition)
    (key, body), = condition.items()
    if key == "always":
        if body is not True:
            raise ValidationFailure(f"{path}: 'always' only accepts true", got=body)
    elif key in {"all", "any"}:
        items = _as_list(body, key)
        for index, item in enumerate(items):
            validate_condition(item, path=f"{path}.{key}[{index}]")
    elif key == "not":
        validate_condition(body, path=f"{path}.not")
    elif key == "fact":
        if not isinstance(body, dict):
            raise ValidationFailure(f"{path}: 'fact' requires a mapping", got=body)
        if not isinstance(body.get("fact"), str) or not body["fact"]:
            raise ValidationFailure(f"{path}: missing non-empty 'fact' name", got=body)
        op = body.get("op", "==")
        if op not in COMPARISONS:
            raise ValidationFailure(f"{path}: unknown comparison", op=op, allowed=sorted(COMPARISONS))
        if "value" not in body:
            raise ValidationFailure(f"{path}: missing comparison 'value'", got=body)
        value = body["value"]
        if not isinstance(value, (int, bool)) or isinstance(value, float):
            raise ValidationFailure(f"{path}: comparison value must be int or bool", got=repr(value))
    else:
        raise ValidationFailure(f"{path}: unknown condition form", form=key)


def referenced_facts(condition: object) -> frozenset[str]:
    """Fact names a condition reads (used for same-time read/write hazard checks)."""
    if condition is None:
        return frozenset()
    if not isinstance(condition, dict) or len(condition) != 1:
        raise ValidationFailure("condition must be a single-key mapping", got=condition)
    (key, body), = condition.items()
    if key == "always":
        return frozenset()
    if key in {"all", "any"}:
        facts: set[str] = set()
        for item in _as_list(body, key):
            facts.update(referenced_facts(item))
        return frozenset(facts)
    if key == "not":
        return referenced_facts(body)
    if key == "fact":
        if not isinstance(body, dict) or not isinstance(body.get("fact"), str):
            raise ValidationFailure("'fact' condition requires a named fact", got=body)
        return frozenset({body["fact"]})
    raise ValidationFailure("unknown condition form", form=key)


def effect_facts(effects: object) -> frozenset[str]:
    """Fact names an effect list writes."""
    if effects is None:
        return frozenset()
    if not isinstance(effects, list):
        raise ValidationFailure("effects must be a list", got=type(effects).__name__)
    facts: set[str] = set()
    for raw in effects:
        if not isinstance(raw, dict) or not isinstance(raw.get("fact"), str):
            raise ValidationFailure("each effect requires a named 'fact'", got=raw)
        facts.add(raw["fact"])
    return frozenset(facts)


def validate_effects(effects: object) -> None:
    if effects is None:
        return
    if not isinstance(effects, list):
        raise ValidationFailure("effects must be a list", got=type(effects).__name__)
    for index, raw in enumerate(effects):
        if not isinstance(raw, dict):
            raise ValidationFailure(f"effects[{index}] must be a mapping", got=raw)
        fact = raw.get("fact")
        if not isinstance(fact, str) or not fact:
            raise ValidationFailure(f"effects[{index}]: missing non-empty 'fact'", got=raw)
        op = raw.get("op", "=")
        if op not in ASSIGNMENTS:
            raise ValidationFailure(f"effects[{index}]: unknown operator", op=op)
        if "value" not in raw:
            raise ValidationFailure(f"effects[{index}]: missing 'value'", got=raw)
        value = raw["value"]
        if not isinstance(value, (int, bool)) or isinstance(value, float):
            raise ValidationFailure(f"effects[{index}]: value must be int or bool", got=repr(value))
        if op in {"+=", "-="} and isinstance(value, bool):
            raise ValidationFailure(f"effects[{index}]: arithmetic on boolean value", fact=fact)
