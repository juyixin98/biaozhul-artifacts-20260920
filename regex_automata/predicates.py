"""Code-point level character predicates.

A :class:`CharPredicate` matches a single Unicode **code point**.
Predicates form a small compositional tree (``Or`` / ``And`` / ``Not`` /
:class:`CharSet`) so that character classes with negated shorthands such
as ``[^\\D]`` keep exact semantics while remaining serialisable to JSON.

Named shorthand classes (``\\d \\w \\s`` and inverses) follow the
documented Unicode policy:

* ``\\d``  code points with Unicode property ``N*`` (``str.isdigit`` plus
  other Number categories; implemented via ``unicodedata.category``)
* ``\\w``  Unicode letters/numbers (``str.isalnum``) plus ``_``
* ``\\s``  ASCII ``\\t \\n \\v \\f \\r`` space plus Unicode ``Z*``
  separator categories

The policy is fixed (there is no ASCII-only flag) and documented in
``docs/language.md``.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Union

_MAX_CP = 0x10FFFF


def is_digit_cp(cp: int) -> bool:
    return unicodedata.category(chr(cp))[0] == "N"


def is_word_cp(cp: int) -> bool:
    ch = chr(cp)
    return ch == "_" or ch.isalnum()


def is_space_cp(cp: int) -> bool:
    if cp in (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20):
        return True
    return unicodedata.category(chr(cp))[0] == "Z"


_NAMED = {
    "digit": is_digit_cp,
    "word": is_word_cp,
    "space": is_space_cp,
}


class CharPredicate:
    """Abstract predicate over a single code point."""

    def matches(self, cp: int) -> bool:
        raise NotImplementedError

    def describe(self) -> object:
        """JSON-serialisable structural description."""
        raise NotImplementedError

    # Convenience constructors ------------------------------------------------
    @staticmethod
    def literal(cp: int) -> "CharPredicate":
        return CharSet(((cp, cp),), ())

    @staticmethod
    def any_char() -> "CharPredicate":
        return Not(CharSet(((0x0A, 0x0A),), ()))  # dot: anything except \n

    @staticmethod
    def named(name: str, negated: bool = False) -> "CharPredicate":
        p: CharPredicate = CharSet((), (name,))
        return Not(p) if negated else p


@dataclass(frozen=True)
class CharSet(CharPredicate):
    """Inclusive codepoint *ranges* and/or named Unicode classes."""

    ranges: tuple[tuple[int, int], ...]
    named: tuple[str, ...]

    def matches(self, cp: int) -> bool:
        for lo, hi in self.ranges:
            if lo <= cp <= hi:
                return True
        for name in self.named:
            if _NAMED[name](cp):
                return True
        return False

    def describe(self) -> object:
        return {
            "kind": "set",
            "ranges": [[lo, hi] for lo, hi in self.ranges],
            "named": list(self.named),
        }


@dataclass(frozen=True)
class Not(CharPredicate):
    inner: CharPredicate

    def matches(self, cp: int) -> bool:
        return not self.inner.matches(cp)

    def describe(self) -> object:
        return {"kind": "not", "inner": self.inner.describe()}


@dataclass(frozen=True)
class Or(CharPredicate):
    left: CharPredicate
    right: CharPredicate

    def matches(self, cp: int) -> bool:
        return self.left.matches(cp) or self.right.matches(cp)

    def describe(self) -> object:
        return {
            "kind": "or",
            "parts": [self.left.describe(), self.right.describe()],
        }


@dataclass(frozen=True)
class And(CharPredicate):
    left: CharPredicate
    right: CharPredicate

    def matches(self, cp: int) -> bool:
        return self.left.matches(cp) and self.right.matches(cp)

    def describe(self) -> object:
        return {
            "kind": "and",
            "parts": [self.left.describe(), self.right.describe()],
        }


PredicateNode = Union[CharSet, Not, Or, And]


def combine_or(parts: list[CharPredicate]) -> CharPredicate:
    """Join positive class items into one predicate (empty list = ∅)."""
    if not parts:
        return CharSet((), ())
    result = parts[0]
    for part in parts[1:]:
        result = Or(result, part)
    return result
