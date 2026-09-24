"""Taint lattice.

Four levels (BOTTOM is internal; reports only ever show clean/maybe/tainted):

    BOTTOM -- "no information / value not defined on this path" (lattice
              bottom, join-neutral).  Never appears in a report.
    CLEAN  -- defined and provably clean on every contributing path
    MAYBE  -- paths differ: some clean, some tainted
    TAINT  -- tainted on every contributing path

Partial order:

    BOTTOM < CLEAN < MAYBE
    BOTTOM < TAINT < MAYBE

Join table:

        | B     C     M     T
    ----+----------------------
    B   | B     C     M     T
    C   | C     C     M     M
    M   | M     M     M     M
    T   | T     M     M     T

The join is associative, commutative, idempotent and monotone.  MAYBE is how
the analyzer says "only on some branches/loop iterations", driving the
false-positive discussion in the README.  TAINT and MAYBE both reach a sink
and are reported; MAYBE findings carry ``certainty: "conditional"``.
"""

from __future__ import annotations

from enum import Enum


class Taint(Enum):
    BOTTOM = "bottom"
    CLEAN = "clean"
    MAYBE = "maybe"
    TAINT = "tainted"

    @staticmethod
    def join(a: "Taint", b: "Taint") -> "Taint":
        if a is b:
            return a
        if a is Taint.BOTTOM:
            return b
        if b is Taint.BOTTOM:
            return a
        if a is b:
            return a
        return Taint.MAYBE


#: levels that count as "possibly reaches a sink"
DANGEROUS = frozenset({Taint.TAINT, Taint.MAYBE})
