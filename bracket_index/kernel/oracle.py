"""Full-stack-scan oracle.

Independent reference implementation: one pass over the whole text with a
single stack. The chunked index must agree with this oracle on every query;
the acceptance tests cross-check the two on synthetic corpora. The oracle
deliberately does not use the summary/composition machinery, so a bug in the
composition algebra cannot silently validate itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..corpus.spec import DEFAULT_SPEC, LexicalSpec
from .lexer import CLOSE, OPEN, Token, lex

# Error categories shared with the API layer.
CATEGORY_BALANCED = "BALANCED"
CATEGORY_TYPE_MISMATCH = "TYPE_MISMATCH"
CATEGORY_UNMATCHED_OPEN = "UNMATCHED_OPEN"
CATEGORY_UNMATCHED_CLOSE = "UNMATCHED_CLOSE"


@dataclass(frozen=True)
class Interval:
    """Half-open character interval [start, end)."""

    start: int
    end: int
    category: str


@dataclass(frozen=True)
class MismatchEvent:
    open: Token
    close: Token


@dataclass(frozen=True)
class ScanResult:
    tokens: tuple[Token, ...]
    pairs: dict[int, int]  # open pos -> close pos and close pos -> open pos
    mismatches: tuple[MismatchEvent, ...]
    unmatched_openers: tuple[Token, ...]
    unmatched_closers: tuple[Token, ...]

    @property
    def balanced(self) -> bool:
        return (
            not self.mismatches
            and not self.unmatched_openers
            and not self.unmatched_closers
        )


def scan(text: str, spec: LexicalSpec = DEFAULT_SPEC) -> ScanResult:
    tokens = lex(text, spec)
    stack: list[Token] = []
    pairs: dict[int, int] = {}
    mismatches: list[MismatchEvent] = []
    unmatched_closers: list[Token] = []
    for tok in tokens:
        if tok.kind == OPEN:
            stack.append(tok)
        elif tok.kind == CLOSE:
            if not stack:
                unmatched_closers.append(tok)
            elif stack[-1].btype == tok.btype:
                opener = stack.pop()
                pairs[opener.pos] = tok.pos
                pairs[tok.pos] = opener.pos
            else:
                mismatches.append(MismatchEvent(open=stack[-1], close=tok))
        else:
            raise ValueError(f"unknown token kind: {tok.kind!r}")
    return ScanResult(
        tokens=tuple(tokens),
        pairs=pairs,
        mismatches=tuple(mismatches),
        unmatched_openers=tuple(stack),
        unmatched_closers=tuple(unmatched_closers),
    )


def shortest_unbalanced_interval(result: ScanResult) -> Interval | None:
    """The first defect, as the minimal interval that evidences it.

    - type mismatch: from the stack-top opener to the offending closer
      (inclusive), the shortest span proving the cross-mismatch;
    - unmatched closer: the closer's own single-character span;
    - unmatched opener: the earliest unmatched opener's single-character span.
    """
    if result.mismatches:
        first = min(result.mismatches, key=lambda event: event.close.pos)
        return Interval(
            start=first.open.pos,
            end=first.close.pos + 1,
            category=CATEGORY_TYPE_MISMATCH,
        )
    if result.unmatched_closers:
        pos = result.unmatched_closers[0].pos
        return Interval(start=pos, end=pos + 1, category=CATEGORY_UNMATCHED_CLOSE)
    if result.unmatched_openers:
        pos = result.unmatched_openers[0].pos
        return Interval(start=pos, end=pos + 1, category=CATEGORY_UNMATCHED_OPEN)
    return None
