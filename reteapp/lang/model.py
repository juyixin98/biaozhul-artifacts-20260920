"""Immutable rule-language model.

A rule has the shape::

    rule "name" salience S:
        on <Type>(constraints..., unique_fields=[...])
        on <Type>(constraints...)        # joined via shared variables
        ...
    action:
        assert <Type>(field = const | ?var, ...)
        retract matched-fact #i
        stop

Constraint kinds:

* :class:`LiteralConstraint`  - field compared against a JSON literal
* :class:`VariableConstraint` - field compared against an already bound variable
* :class:`Binding`            - field must exist; its value is bound to a name

Bindings are the join keys: two CEs constraining the same variable require the
joined facts to carry equal field values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Operator(str, Enum):
    EQ = "=="
    NEQ = "!="
    LT = "<"
    LTE = "<="
    GT = ">"
    GTE = ">="
    IN = "in"
    NOT_IN = "not_in"

    @classmethod
    def from_raw(cls, raw: str) -> "Operator":
        try:
            return cls(raw)
        except ValueError as exc:
            allowed = ", ".join(op.value for op in cls)
            raise ValueError(f"unknown operator {raw!r}; expected one of: {allowed}") from exc


@dataclass(frozen=True)
class LiteralConstraint:
    field: str
    op: Operator
    value: Any  # JSON literal: str|int|float|bool|None|list|dict


@dataclass(frozen=True)
class VariableConstraint:
    """Predicate test against a variable bound earlier (same CE or prior CE)."""

    field: str
    op: Operator
    variable: str


@dataclass(frozen=True)
class Binding:
    """Field existence test that introduces (or re-introduces) a variable.

    ``required=False`` marks a soft/left binding that the compiler rejects for
    the first CE but could be used by future negation constructs; today every
    binding is required.
    """

    field: str
    variable: str
    required: bool = True


Constraint = LiteralConstraint | VariableConstraint | Binding


@dataclass(frozen=True)
class ConditionalElement:
    """One alpha pattern ("row" in the condition list)."""

    type: str
    constraints: tuple[Constraint, ...]


@dataclass(frozen=True)
class AssertTemplate:
    type: str
    # field name -> either {"value": <literal>} or {"variable": "x"}
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Action:
    asserts: tuple[AssertTemplate, ...] = ()
    # Indices of matched CEs whose facts are retracted when the rule fires.
    retract_ce_indices: tuple[int, ...] = ()
    stop: bool = False


@dataclass(frozen=True)
class Rule:
    name: str
    conditions: tuple[ConditionalElement, ...]
    action: Action
    salience: int = 0
    enabled: bool = True
    # Refraction: once an activation has fired, the *same physical token* may
    # not fire again until it has been removed (e.g. by retract) and rebuilt.
    refraction: bool = True
