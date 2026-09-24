"""Kleene three-valued logic.

Attribute access has three outcomes:

* ``TRUE`` / ``FALSE`` — the attribute exists and the predicate decided;
* ``UNKNOWN`` — a referenced attribute was missing, or an ``in``/``subset``
  test could not be decided because a referenced set was missing.

UNKNOWN is *not* a third permission: the decision engine treats an UNKNOWN
rule condition as non-permitting (a missing attribute can never grant
access). These helpers only implement the truth tables; the "deny on
unknown" policy lives in :mod:`app.evaluator`.
"""

from __future__ import annotations

from enum import Enum


class Tri(Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"

    @property
    def is_true(self) -> bool:
        return self is Tri.TRUE

    def as_decision_bool(self) -> bool:
        """Whether the value is definitively TRUE (UNKNOWN -> False)."""
        return self is Tri.TRUE


def tri_not(v: Tri) -> Tri:
    if v is Tri.TRUE:
        return Tri.FALSE
    if v is Tri.FALSE:
        return Tri.TRUE
    return Tri.UNKNOWN


def tri_and(a: Tri, b: Tri) -> Tri:
    # False dominates; unknown only "infects" when nothing is false.
    if a is Tri.FALSE or b is Tri.FALSE:
        return Tri.FALSE
    if a is Tri.UNKNOWN or b is Tri.UNKNOWN:
        return Tri.UNKNOWN
    return Tri.TRUE


def tri_or(a: Tri, b: Tri) -> Tri:
    # True dominates; unknown only "infects" when nothing is true.
    if a is Tri.TRUE or b is Tri.TRUE:
        return Tri.TRUE
    if a is Tri.UNKNOWN or b is Tri.UNKNOWN:
        return Tri.UNKNOWN
    return Tri.FALSE
