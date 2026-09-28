"""Domain entities for the STRIPS planning service.

The rule language is *lifted*: action schemas mention parameters whose types
are declared in the problem. Validation (:mod:`strips_planner.language`) grounds
every schema into :class:`GroundAction` instances consumed by the planner and
the independent executor.

All entities are immutable. States are never represented here; see
:mod:`strips_planner.core.state`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from strips_planner.errors import ValidationIssue


@dataclass(frozen=True, slots=True)
class Atom:
    """A ground or lifted positive literal, e.g. ``(at camp)`` or ``(at to)``."""

    predicate: str
    args: tuple[str, ...] = ()

    def __str__(self) -> str:
        if self.args:
            return f"({self.predicate} {' '.join(self.args)})"
        return f"({self.predicate})"


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    type_name: str


@dataclass(frozen=True, slots=True)
class PredicateDecl:
    name: str
    types: tuple[str, ...]
    static: bool


@dataclass(frozen=True, slots=True)
class ActionSchema:
    name: str
    parameters: tuple[Parameter, ...]
    pre_pos: tuple[Atom, ...]
    pre_neg: tuple[Atom, ...]
    add: tuple[Atom, ...]
    delete: tuple[Atom, ...]
    cost: float


@dataclass(frozen=True, slots=True)
class Problem:
    """A fully validated and grounded STRIPS problem."""

    name: str
    types: frozenset[str]
    objects: dict[str, tuple[str, ...]]
    predicates: dict[str, PredicateDecl]
    init: frozenset[Atom]
    goal_pos: frozenset[Atom]
    goal_neg: frozenset[Atom]
    schemas: tuple[ActionSchema, ...]
    ground_actions: tuple["GroundAction", ...]
    notes: tuple[str, ...] = field(default=())
    warnings: tuple[ValidationIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class GroundAction:
    """An action schema specialised with one concrete type-correct binding."""

    label: str
    schema_name: str
    binding: tuple[tuple[str, str], ...]
    pre_pos: frozenset[Atom]
    pre_neg: frozenset[Atom]
    add: frozenset[Atom]
    delete: frozenset[Atom]
    cost: float

    def __str__(self) -> str:
        return self.label
