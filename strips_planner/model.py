"""Core language data model.

Representation choices (part of the inter-module contract):

* A ground (or parameterised) atom is a ``tuple[str, ...]`` whose first element
  is the predicate and whose remaining elements are arguments::

      ("at", "w1", "bench")

* States are ``frozenset[Atom]`` of *true* atoms: closed-world semantics,
  so a negative literal is matched by absence from the set.
* All dataclasses are frozen; transitions always build new state objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# An atom: (predicate, arg0, arg1, ...).
Atom = tuple[str, ...]


@dataclass(frozen=True)
class ActionSchema:
    """A lifted STRIPS operator (parameters are variable names)."""

    name: str
    parameters: tuple[str, ...]
    pre_pos: frozenset[Atom]
    pre_neg: frozenset[Atom]
    add_effects: frozenset[Atom]
    del_effects: frozenset[Atom]
    cost: int = 1


@dataclass(frozen=True)
class Domain:
    name: str
    actions: tuple[ActionSchema, ...]
    predicate_arity: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Problem:
    name: str
    objects: tuple[str, ...]
    initial: frozenset[Atom]
    goal_pos: frozenset[Atom]
    goal_neg: frozenset[Atom]


@dataclass(frozen=True)
class GroundAction:
    """An operator instance with every parameter replaced by an object."""

    schema_name: str
    args: tuple[str, ...]
    pre_pos: frozenset[Atom]
    pre_neg: frozenset[Atom]
    add_effects: frozenset[Atom]
    del_effects: frozenset[Atom]
    cost: int

    @property
    def signature(self) -> str:
        return atom_text((self.schema_name, *self.args))


State = frozenset[Atom]


def atom_text(atom: Atom) -> str:
    """Canonical human-readable form of an atom: ``pred(a, b)``."""
    if len(atom) == 1:
        return atom[0]
    return f"{atom[0]}({', '.join(atom[1:])})"


def state_to_texts(state: State) -> list[str]:
    return [atom_text(a) for a in sorted(state)]
