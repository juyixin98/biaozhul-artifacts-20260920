"""Core value types for the ATMS kernel.

An *environment* is a set of assumption names under which a node holds.
A *label* is the set of environments supporting a node, kept subset-minimal
and free of nogood supersets by the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# An environment is an immutable set of assumption names.
Environment = frozenset[str]

#: Name of the distinguished contradiction node. Any environment that
#: supports it is a nogood.
CONTRADICTION = "⊥"


def is_nogood_superset(env: Environment, nogoods: set[Environment]) -> bool:
    """True when ``env`` contains a known nogood (and is therefore invalid)."""
    return any(ng <= env for ng in nogoods)


def minimize(envs: set[Environment]) -> set[Environment]:
    """Return the subset-minimal members of ``envs`` (no two comparable)."""
    ordered = sorted(envs, key=len)
    kept: list[Environment] = []
    for env in ordered:
        if not any(prev <= env for prev in kept):
            kept.append(env)
    return set(kept)


@dataclass(frozen=True)
class Rule:
    """A Horn-style justification: all antecedents together imply consequent."""

    rule_id: str
    antecedents: tuple[str, ...]
    consequent: str


@dataclass
class OpResult:
    """Outcome of one engine operation, for diagnostics and API responses."""

    accepted: bool
    reason: str
    detail: dict = field(default_factory=dict)
