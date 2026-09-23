"""Abstract syntax tree for the regex subset.

All nodes carry a source :class:`~regex_automata.locations.Span`.  The AST
is deliberately small — one node per documented grammar production.
"""
from __future__ import annotations

from dataclasses import dataclass

from .locations import Span
from .predicates import CharPredicate


@dataclass(frozen=True)
class Node:
    span: Span


@dataclass(frozen=True)
class Empty(Node):
    """The empty pattern (matches the empty string)."""


@dataclass(frozen=True)
class Char(Node):
    cp: int


@dataclass(frozen=True)
class Class(Node):
    """``[...]`` or a shorthand such as ``\\d`` — matches one code point."""

    predicate: CharPredicate


@dataclass(frozen=True)
class Dot(Node):
    """``.`` — any code point except ``\\n``."""


@dataclass(frozen=True)
class Anchor(Node):
    """Zero-width assertion; ``kind`` is ``^``, ``$``, ``b`` or ``B``."""

    kind: str


@dataclass(frozen=True)
class Concat(Node):
    parts: tuple[Node, ...]


@dataclass(frozen=True)
class Alt(Node):
    branches: tuple[Node, ...]


@dataclass(frozen=True)
class Repeat(Node):
    """``child{minimum,maximum}``; ``maximum=None`` means unbounded."""

    child: Node
    minimum: int
    maximum: int | None


@dataclass(frozen=True)
class Group(Node):
    """A parenthesised subexpression. Semantically transparent, but distinct
    from its child so the parser can tell ``(a*)*`` (valid) from ``a**`` (not).
    """

    child: Node
