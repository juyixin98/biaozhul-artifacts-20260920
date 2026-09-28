"""Safe chunk boundaries.

Blocks must never be split in the middle of a quoted string, an escape
sequence, or a comment -- otherwise a bracket masked in one block would be
re-lexed as structural in the next. Boundaries are chosen using the span list
of a single lex pass over the text being chunked.
"""
from __future__ import annotations

from bisect import bisect_right

from .lexer import Lexer, MaskedSpan


def chunk_text(text: str, lexer: Lexer, target: int) -> list[str]:
    """Split ``text`` into chunks of roughly ``target`` chars.

    A boundary at position ``p`` is legal when no span covers ``p`` (i.e.
    ``p`` is not inside ``[start, end)`` of any masked span). If the target
    lands inside a span, the nearer of the span's two edges is used; if a
    span runs to EOF, a chunk may overrun the target -- correctness beats
    sizing.
    """
    if target < 1:
        raise ValueError("target chunk size must be >= 1")
    if not text:
        return []
    _, spans = lexer.scan(text)
    starts = [s.start for s in spans]
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        candidate = min(n, start + target)
        if candidate < n:
            candidate = _nearest_legal_boundary(
                candidate, start + 1, n, spans, starts
            )
        chunks.append(text[start:candidate])
        start = candidate
    return chunks


def _nearest_legal_boundary(
    candidate: int, minimum: int, n: int,
    spans: list[MaskedSpan], starts: list[int],
) -> int:
    """Nearest legal boundary to ``candidate`` within [minimum, n].

    Both edges of the covering span are legal; take the nearer one. EOF is
    always legal and handles a span that never terminates.
    """
    covering = _covering_span(candidate, spans, starts)
    if covering is None:
        return candidate
    options: list[int] = []
    # The span's left edge is a legal boundary only if it is strictly inside
    # the block (a span opening exactly at the block start leaves no legal
    # cut before its right edge).
    if covering.start >= minimum:
        options.append(covering.start)
    # span.end may equal n (EOF), which is always a legal boundary.
    options.append(min(n, covering.end))
    return min(options, key=lambda p: abs(p - candidate))


def _covering_span(
    pos: int, spans: list[MaskedSpan], starts: list[int]
) -> MaskedSpan | None:
    """Span whose half-open interval [start, end) contains ``pos``."""
    idx = bisect_right(starts, pos) - 1
    if idx < 0:
        return None
    span = spans[idx]
    return span if span.start <= pos < span.end else None
