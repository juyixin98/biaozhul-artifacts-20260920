"""Complete stack scan -- the reference path.

This is a deliberately simple, whole-document streaming stack scan over the
lexer output. The indexed chunk pipeline (``tokens.compose`` over a treap of
blocks) is cross-checked against it by :func:`analyze_text` verification and
by the API ``/query/verify`` endpoint.

Note the test suite contains a SECOND, independently written oracle
(``tests/fixtures/oracle.py``) plus hand-authored expectations; this module is
part of the product, not the test oracle.
"""
from __future__ import annotations

from dataclasses import dataclass

from .lexer import CLOSE, OPEN, Token
from .tokens import Pair, reduce_tokens


@dataclass(frozen=True)
class Defect:
    """One structural defect with a concrete interval and failure category.

    Categories:
    * ``TYPE_MISMATCH`` -- closer met a differently typed opener;
    * ``STRAY_CLOSE``   -- closer with an empty stack;
    * ``STRAY_OPEN``    -- opener never closed before document end.
    """

    category: str
    type: str
    start: int
    end: int  # exclusive
    open_offset: int | None
    close_offset: int | None

    def as_state(self) -> dict:
        return {
            "category": self.category,
            "type": self.type,
            "interval": [self.start, self.end],
            "open_offset": self.open_offset,
            "close_offset": self.close_offset,
        }


TYPE_MISMATCH = "TYPE_MISMATCH"
STRAY_CLOSE = "STRAY_CLOSE"
STRAY_OPEN = "STRAY_OPEN"


@dataclass(frozen=True)
class StructureResult:
    matches: tuple[Pair, ...]
    mismatches: tuple[Pair, ...]
    stray_closes: tuple[Token, ...]
    stray_opens: tuple[Token, ...]
    length: int

    @property
    def balanced(self) -> bool:
        return not (self.mismatches or self.stray_closes or self.stray_opens)

    def match_for(self, offset: int) -> Pair | None:
        for o, c in self.matches:
            if o.offset == offset or c.offset == offset:
                return o, c
        return None

    def defect_for(self, offset: int) -> Defect | None:
        for d in self.defects():
            if d.open_offset == offset or d.close_offset == offset:
                return d
        return None

    def defects(self) -> list[Defect]:
        out: list[Defect] = []
        for o, c in self.mismatches:
            out.append(
                Defect(TYPE_MISMATCH, o.type, o.offset, c.offset + 1,
                       o.offset, c.offset)
            )
        for c in self.stray_closes:
            out.append(
                Defect(STRAY_CLOSE, c.type, c.offset, c.offset + 1,
                       None, c.offset)
            )
        # An unmatched opener can only be explained by "no closer before
        # EOF", so its interval runs to document end.
        for o in self.stray_opens:
            out.append(
                Defect(STRAY_OPEN, o.type, o.offset, self.length,
                       o.offset, None)
            )
        out.sort(key=lambda d: (d.start, d.end))
        return out

    def shortest_unbalanced_interval(self) -> Defect | None:
        defects = self.defects()
        if not defects:
            return None
        # Minimum interval length; earliest start breaks ties.
        return min(defects, key=lambda d: (d.end - d.start, d.start))


def scan_tokens(tokens: list[Token], length: int) -> StructureResult:
    """Naive O(n) complete stack scan."""
    r = reduce_tokens(tokens)
    # reduce_tokens already separates the three failure classes exactly the
    # way the streaming scan does; re-deriving them here keeps this path's
    # code shape independent from the composition code.
    stack: list[Token] = []
    matches: list[Pair] = []
    mismatches: list[Pair] = []
    stray_closes: list[Token] = []
    for tok in tokens:
        if tok.kind == OPEN:
            stack.append(tok)
        elif not stack:
            stray_closes.append(tok)
        else:
            top = stack.pop()
            if top.type == tok.type:
                matches.append((top, tok))
            else:
                mismatches.append((top, tok))
    assert len(r.prefix) == len(stray_closes)
    return StructureResult(
        matches=tuple(matches),
        mismatches=tuple(mismatches),
        stray_closes=tuple(stray_closes),
        stray_opens=tuple(stack),
        length=length,
    )
