"""Quote/escape/comment aware lexer.

The lexer is the *only* place that decides which characters are structural.
Bracket characters inside a quoted span or a comment are emitted as nothing;
escape handling follows the quote declaration rather than ad-hoc string
slicing.

Offsets returned by :meth:`Lexer.scan` are relative to the start of ``text``;
callers that scan a storage block add the block start themselves. Keeping
offsets relative here is what lets an edit shift later blocks without
re-lexing them.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..corpus import BracketLexicon

OPEN = "open"
CLOSE = "close"


@dataclass(frozen=True)
class Token:
    """One structural bracket.

    ``offset`` is relative to the unit the token was scanned from (a block for
    the index, the whole document for the stateless query path).
    """

    offset: int
    kind: str
    type: str
    char: str


@dataclass(frozen=True)
class MaskedSpan:
    """A region whose contents carry no bracket structure.

    ``kind`` is ``"quote"`` or ``"comment"``; ``terminated`` is False for a
    quote that runs to end of input or a block comment that never closes.
    """

    kind: str
    name: str
    start: int
    end: int  # exclusive
    terminated: bool


@dataclass(frozen=True)
class OpenMask:
    """State carried across a block boundary: an unterminated mask.

    Only quotes and *block* comments cross boundaries (line comments end at
    the newline). Holding the spec gives the continued scan its delimiter,
    escape rule and closing sequence without guessing.
    """

    kind: str  # "quote" | "block"
    spec: object

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OpenMask):
            return NotImplemented
        return self.kind == other.kind and self.spec == other.spec


class Lexer:
    def __init__(self, lexicon: BracketLexicon) -> None:
        self._lexicon = lexicon
        self._opens = lexicon.open_map()
        self._closes = lexicon.close_map()
        self._quotes = lexicon.quote_map()
        self._comments = lexicon.comment_map()
        # Longest comment opener first, so "/*/" style prefixes resolve
        # deterministically.
        self._comment_prefixes = sorted(self._comments, key=len, reverse=True)

    @property
    def lexicon(self) -> BracketLexicon:
        return self._lexicon

    def scan(self, text: str) -> tuple[list[Token], list[MaskedSpan]]:
        """Return structural tokens and masked spans found in ``text``."""
        tokens, spans, _ = self.scan_block(text, None)
        return tokens, spans

    def scan_block(
        self, text: str, initial: "OpenMask | None"
    ) -> tuple[list[Token], list[MaskedSpan], "OpenMask | None"]:
        """Scan one storage block, possibly continuing a mask from the left.

        ``initial`` is the mask state entering position 0 (from the previous
        block); the returned tail is the mask still open at ``len(text)``.
        Tokens keep block-relative offsets.
        """
        tokens: list[Token] = []
        spans: list[MaskedSpan] = []
        n = len(text)
        i = 0
        if initial is not None:
            i, tail = self._continue_mask(text, 0, initial)
            if tail is not None:
                spans.append(
                    MaskedSpan(initial.kind, initial.spec.name, 0, n, False)
                )
                return tokens, spans, tail
            if i > 0:
                spans.append(
                    MaskedSpan(initial.kind, initial.spec.name, 0, i, True)
                )
        while i < n:
            ch = text[i]
            if ch in self._quotes:
                spec = self._quotes[ch]
                end, terminated = self._consume_quote(text, i, spec)
                spans.append(MaskedSpan("quote", spec.name, i, end, terminated))
                i = end
                if not terminated:
                    return tokens, spans, OpenMask("quote", spec)
                continue
            comment = self._match_comment_prefix(text, i)
            if comment is not None:
                end, terminated = self._consume_comment(text, i, comment)
                spans.append(
                    MaskedSpan("comment", comment.name, i, end, terminated)
                )
                i = end
                if not terminated:
                    return tokens, spans, OpenMask("block", comment)
                continue
            if ch in self._opens:
                type_name, _ = self._opens[ch]
                tokens.append(Token(i, OPEN, type_name, ch))
            elif ch in self._closes:
                type_name, _ = self._closes[ch]
                tokens.append(Token(i, CLOSE, type_name, ch))
            i += 1
        return tokens, spans, None

    def _continue_mask(
        self, text: str, start: int, mask: "OpenMask"
    ) -> tuple[int, "OpenMask | None"]:
        """Resume scanning inside a mask that opened in a previous block.

        Returns ``(position, tail)``: position is the first unmasked offset
        (== len(text) when the mask stays open); tail is non-None iff the
        mask remains open at end of block.
        """
        n = len(text)
        if mask.kind == "quote":
            i = start
            spec = mask.spec
            while i < n:
                ch = text[i]
                if spec.escape_char is not None and ch == spec.escape_char:
                    i += 2
                    continue
                if ch == spec.delimiter:
                    return i + 1, None
                i += 1
            return n, OpenMask("quote", spec)
        # Block comment: look only for its closing sequence; nothing else is
        # structural while inside.
        spec = mask.spec
        end = text.find(spec.closing, start)
        if end == -1:
            return n, OpenMask("block", spec)
        return end + len(spec.closing), None

    def ends_in_open_mask(self, text: str) -> bool:
        """True iff the last character of ``text`` is inside an unclosed
        quote or block comment. Used by the editor to decide whether an edit
        forced a mask across the block boundary."""
        _, spans, tail = self.scan_block(text, None)
        return tail is not None

    def _consume_quote(self, text: str, start: int, spec) -> tuple[int, bool]:
        """Scan a quote starting at ``text[start] == spec.delimiter``.

        With an escape char, ``\\<delimiter>`` and ``\\\\`` do not close the
        span; the escape consumes exactly one following character (per spec).
        A quote that never closes runs to end of input: its contents are still
        masked, and ``terminated=False`` lets diagnostics flag the fact.
        """
        n = len(text)
        i = start + 1
        while i < n:
            ch = text[i]
            if spec.escape_char is not None and ch == spec.escape_char:
                # Escape consumes the next char; a trailing escape cannot
                # close the span either.
                i += 2
                continue
            if ch == spec.delimiter:
                return i + 1, True
            i += 1
        return n, False

    def _match_comment_prefix(self, text: str, pos: int):
        for prefix in self._comment_prefixes:
            if text.startswith(prefix, pos):
                return self._comments[prefix]
        return None

    def _consume_comment(self, text: str, start: int, spec) -> tuple[int, bool]:
        n = len(text)
        if spec.closing is None:
            nl = text.find("\n", start + len(spec.opening))
            return (n if nl == -1 else nl + 1), True
        end = text.find(spec.closing, start + len(spec.opening))
        if end == -1:
            return n, False
        return end + len(spec.closing), True
