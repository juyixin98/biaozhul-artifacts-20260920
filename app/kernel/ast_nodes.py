"""Immutable regex AST nodes and the emptiness (nullable) analysis."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Char:
    """A set of characters as sorted disjoint inclusive ranges."""

    ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Epsilon:
    """The empty string."""


@dataclass(frozen=True)
class Concat:
    parts: tuple[Node, ...]


@dataclass(frozen=True)
class Alt:
    options: tuple[Node, ...]


@dataclass(frozen=True)
class Star:
    node: Node


@dataclass(frozen=True)
class Plus:
    node: Node


@dataclass(frozen=True)
class Opt:
    node: Node


@dataclass(frozen=True)
class Repeat:
    node: Node
    min: int
    max: int | None  # None means unbounded


Node = Char | Epsilon | Concat | Alt | Star | Plus | Opt | Repeat


def nullable(node: Node) -> bool:
    """True iff the language of ``node`` contains the empty string."""
    if isinstance(node, Char):
        return False
    if isinstance(node, Epsilon):
        return True
    if isinstance(node, Concat):
        return all(nullable(part) for part in node.parts)
    if isinstance(node, Alt):
        return any(nullable(opt) for opt in node.options)
    if isinstance(node, (Star, Opt)):
        return True
    if isinstance(node, Plus):
        return nullable(node.node)
    if isinstance(node, Repeat):
        return node.min == 0 or nullable(node.node)
    raise TypeError(f"unknown AST node: {node!r}")


def count_nodes(node: Node) -> int:
    """Number of syntactic AST nodes (before repeat expansion)."""
    if isinstance(node, (Char, Epsilon)):
        return 1
    if isinstance(node, Concat):
        return 1 + sum(count_nodes(p) for p in node.parts)
    if isinstance(node, Alt):
        return 1 + sum(count_nodes(o) for o in node.options)
    if isinstance(node, (Star, Plus, Opt)):
        return 1 + count_nodes(node.node)
    if isinstance(node, Repeat):
        return 1 + count_nodes(node.node)
    raise TypeError(f"unknown AST node: {node!r}")
