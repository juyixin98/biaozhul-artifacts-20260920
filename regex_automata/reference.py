"""Reference interpreter — a *test oracle*, not part of the shipped engine.

It recognises the same language with a deliberately different algorithm
from the Thompson engine: a memoised Boolean relation

    can(node, i, j)  <=>  ``node`` can consume exactly text[i:j]

with explicit split enumeration for concatenation and repetition.  It is
simple (and exponential in the worst case) and is only ever run on tiny
inputs in tests.

Greedy-vs-lazy quantifier order does not change which strings are
accepted, so the oracle answers the same leftmost-longest question that
the NFA simulation answers:

* ``search``    earliest start ``s`` with any accepting end, then the
                largest such end
* ``prefix``    largest ``e`` for which ``can(tree, 0, e)``
* ``fullmatch`` ``can(tree, 0, len(text))``
"""
from __future__ import annotations

from . import ast
from .matcher import Match
from .predicates import is_word_cp


class ReferenceMatcher:
    def __init__(self, tree: ast.Node, text: str) -> None:
        self.tree = tree
        self.text = text
        self.n = len(text)
        self.memo: dict[tuple[int, int, int], bool] = {}

    # exact-span recognition ---------------------------------------------------
    def can(self, node: ast.Node, i: int, j: int) -> bool:
        key = (id(node), i, j)
        if key in self.memo:
            return self.memo[key]
        result = self._compute(node, i, j)
        self.memo[key] = result
        return result

    def _compute(self, node: ast.Node, i: int, j: int) -> bool:
        text = self.text
        if isinstance(node, ast.Empty):
            return i == j
        if isinstance(node, ast.Char):
            return j - i == 1 and ord(text[i]) == node.cp
        if isinstance(node, ast.Dot):
            return j - i == 1 and text[i] != "\n"
        if isinstance(node, ast.Class):
            return j - i == 1 and node.predicate.matches(ord(text[i]))
        if isinstance(node, ast.Anchor):
            if i != j:
                return False
            if node.kind == "^":
                return i == 0
            if node.kind == "$":
                return i == self.n
            before = is_word_cp(ord(text[i - 1])) if i > 0 else False
            after = is_word_cp(ord(text[i])) if i < self.n else False
            boundary = before != after
            return boundary if node.kind == "b" else not boundary
        if isinstance(node, ast.Alt):
            return any(self.can(b, i, j) for b in node.branches)
        if isinstance(node, ast.Group):
            return self.can(node.child, i, j)
        if isinstance(node, ast.Concat):
            return self._seq(node.parts, 0, i, j)
        if isinstance(node, ast.Repeat):
            return self._repeat(node, i, j)
        raise TypeError(f"unknown node {node!r}")

    def _seq(self, parts: tuple[ast.Node, ...], p: int, i: int, j: int) -> bool:
        if p == len(parts):
            return i == j
        head = parts[p]
        for m in range(i, j + 1):
            if self.can(head, i, m) and self._seq(parts, p + 1, m, j):
                return True
        return False

    def _repeat(self, node: ast.Repeat, i: int, j: int) -> bool:
        """``child{lo,hi}`` recognised by choosing child copies sequentially."""
        optional = None if node.maximum is None else node.maximum - node.minimum
        return self._copies(node.child, node.minimum, optional, i, j)

    def _copies(
        self,
        child: ast.Node,
        mandatory_left: int,
        optional_left: int | None,
        i: int,
        j: int,
    ) -> bool:
        if mandatory_left > 0:
            # This copy must happen (possibly zero-width).
            for m in range(i, j + 1):
                if self.can(child, i, m) and self._copies(
                    child, mandatory_left - 1, optional_left, m, j
                ):
                    return True
            return False
        # All mandatory copies placed.
        if optional_left is not None:
            if optional_left == 0:
                return i == j
            if i == j:
                return True  # stop; remaining optional copies skipped
            for m in range(i, j + 1):
                if self.can(child, i, m) and self._copies(
                    child, 0, optional_left - 1, m, j
                ):
                    return True
            return False
        # Unbounded tail: another copy, or stop.  Require positive progress per
        # extra copy so a zero-width child cannot spin forever (same language as
        # the engine's loop, which makes progress before revisiting a choice).
        if i == j:
            return True  # stop
        for m in range(i + 1, j + 1):
            if self.can(child, i, m) and self._copies(child, 0, None, m, j):
                return True
        return False

    # public API ---------------------------------------------------------------
    def search(self) -> Match | None:
        for s in range(self.n + 1):
            for e in range(self.n, s - 1, -1):
                if self.can(self.tree, s, e):
                    return Match(s, e, self.text[s:e])
        return None

    def prefix(self) -> Match | None:
        for e in range(self.n, -1, -1):
            if self.can(self.tree, 0, e):
                return Match(0, e, self.text[:e])
        return None

    def fullmatch(self) -> Match | None:
        if self.can(self.tree, 0, self.n):
            return Match(0, self.n, self.text)
        return None
