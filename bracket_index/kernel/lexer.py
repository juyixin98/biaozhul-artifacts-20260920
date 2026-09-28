"""Bracket lexer.

Scans text left to right and emits bracket tokens, honouring the lexical
spec's quote and escape ranges: while inside a string literal, no bracket
tokens are produced. An escape character inside a string consumes the next
character unconditionally (so `\\"` does not close the string, and `\\\\`
is an escaped backslash).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..corpus.spec import DEFAULT_SPEC, LexicalSpec

OPEN = "open"
CLOSE = "close"


@dataclass(frozen=True)
class Token:
    kind: str  # OPEN or CLOSE
    btype: str  # canonical bracket type: the opening symbol, e.g. "("
    pos: int  # offset of the bracket character in the scanned text


def lex_with_state(
    text: str,
    spec: LexicalSpec = DEFAULT_SPEC,
    base: int = 0,
    in_quote: str | None = None,
    escape_pending: bool = False,
) -> tuple[list[Token], str | None, bool]:
    """Tokenize `text` starting with lexer state (`in_quote`, `escape_pending`).

    - `in_quote`: quote character whose string is currently open, or None.
    - `escape_pending`: the first character of `text` must be consumed as the
      escaped character of an open string (the previous chunk ended on an
      odd-length run of backslashes).

    Returns tokens plus the exit state, so a chunking caller threads both
    pieces across chunk boundaries. Positions are offset by `base`.
    """
    tokens: list[Token] = []
    open_to_close = spec.open_to_close
    close_to_open = spec.close_to_open
    quotes = spec.quotes
    escape = spec.escape
    i = 0
    n = len(text)
    loop_start = 0
    if in_quote is not None and escape_pending and n > 0:
        # Escaped character — may be a quote, backslash or bracket; consumed
        # unconditionally without changing quote state or emitting a token.
        i = 1
        loop_start = 1
    while i < n:
        ch = text[i]
        if in_quote is not None:
            if ch == escape:
                i += 2
                continue
            if ch == in_quote:
                in_quote = None
            i += 1
            continue
        if ch in quotes:
            in_quote = ch
        elif ch in open_to_close:
            tokens.append(Token(OPEN, ch, base + i))
        elif ch in close_to_open:
            tokens.append(Token(CLOSE, close_to_open[ch], base + i))
        i += 1
    exit_escape_pending = False
    if in_quote is not None and n > loop_start:
        # Only the suffix actually processed by this chunk's loop can leave a
        # dangling escape; the consumed first character (if any) was itself
        # the escape TARGET, not an escape introducer.
        run = 0
        j = n - 1
        while j >= loop_start and text[j] == escape:
            run += 1
            j -= 1
        exit_escape_pending = run % 2 == 1
    return tokens, in_quote, exit_escape_pending


def lex(text: str, spec: LexicalSpec = DEFAULT_SPEC, base: int = 0) -> list[Token]:
    """Tokenize `text` (starting outside any string); positions offset by `base`."""
    tokens, _, _ = lex_with_state(text, spec, base)
    return tokens
