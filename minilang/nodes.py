"""Syntax tree nodes and structural utilities.

All spans are half-open character offsets ``[start, end)`` into the source
text.  Sub-spans inside ``value`` are stored as plain dicts with the keys
``"start"`` / ``"end"`` so that :func:`shift_node` can walk them generically.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class Node:
    """A single syntax node.

    Attributes:
        kind: node type, e.g. ``"let"`` / ``"binary"`` / ``"number"``.
        start, end: character offsets spanning the node.
        value: extra attributes (operator text, identifier name, flags...).
        children: child nodes in source order.
    """

    __slots__ = ("kind", "start", "end", "value", "children")

    def __init__(
        self,
        kind: str,
        start: int,
        end: int,
        value: Optional[Dict[str, Any]] = None,
        children: Optional[List["Node"]] = None,
    ) -> None:
        self.kind = kind
        self.start = start
        self.end = end
        self.value: Dict[str, Any] = value or {}
        self.children: List[Node] = children or []

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Node({self.kind!r}, {self.start}..{self.end})"


def shift_node(node: Node, delta: int) -> None:
    """Shift every offset in a reused subtree by *delta* (mutates in place).

    After an edit, a node whose source text is unchanged but whose position
    moved (the edit is entirely before it) keeps its identity; only its
    offsets are translated.
    """
    if delta == 0:
        return
    node.start += delta
    node.end += delta
    _shift_value(node.value, delta)
    for child in node.children:
        shift_node(child, delta)


def _shift_value(value: Any, delta: int) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("start", "end") and isinstance(item, int):
                value[key] = item + delta
            else:
                _shift_value(item, delta)
    elif isinstance(value, list):
        for item in value:
            _shift_value(item, delta)


def nodes_equal(a: Node, b: Node) -> bool:
    """Deep structural equality: kinds, spans, values and children all match.

    Used to prove incremental output equals a fresh full parse.
    """
    if not (a.kind == b.kind and a.start == b.start and a.end == b.end):
        return False
    if a.value != b.value:
        return False
    if len(a.children) != len(b.children):
        return False
    return all(nodes_equal(x, y) for x, y in zip(a.children, b.children))


def tree_signature(node: Node) -> tuple:
    """Hashable structural representation, handy for readable test failures."""
    return (
        node.kind,
        node.start,
        node.end,
        tuple(sorted((k, _value_signature(v)) for k, v in node.value.items())),
        tuple(tree_signature(c) for c in node.children),
    )


def _value_signature(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _value_signature(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_value_signature(v) for v in value)
    return value
