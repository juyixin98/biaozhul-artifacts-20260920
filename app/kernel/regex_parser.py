"""Recursive-descent parser for the rule regex dialect.

Grammar::

    regex   := alt
    alt     := concat ('|' concat)*
    concat  := repeat*
    repeat  := atom ('*' | '+' | '?' | '{'m[','n]'}')*
    atom    := '(' alt ')' | '[' class ']' | '.' | escape | literal

Syntax errors raise ``AppError(INPUT_ERROR)``; repeat counts above the
configured limit raise ``AppError(RESOURCE_EXHAUSTED)``.
"""

from __future__ import annotations

from ..corpus.spec import (
    DIGIT_RANGES,
    DOT_RANGES,
    SPACE_RANGES,
    WORD_RANGES,
    complement_ranges,
    normalize_ranges,
)
from ..errors import input_error, resource_exhausted
from .ast_nodes import Alt, Char, Concat, Epsilon, Node, Opt, Plus, Repeat, Star

_META = set("*+?{}[]()|.")

_CLASS_ESCAPES = {
    "d": DIGIT_RANGES,
    "w": WORD_RANGES,
    "s": SPACE_RANGES,
    "D": complement_ranges(DIGIT_RANGES),
    "W": complement_ranges(WORD_RANGES),
    "S": complement_ranges(SPACE_RANGES),
}

_CONTROL_ESCAPES = {
    "n": 0x0A,
    "t": 0x09,
    "r": 0x0D,
    "f": 0x0C,
    "v": 0x0B,
}


def parse(pattern: str, *, max_repeat: int) -> Node:
    """Parse ``pattern`` into an AST, enforcing the repeat-count limit."""
    parser = _Parser(pattern, max_repeat=max_repeat)
    return parser.parse()


class _Parser:
    def __init__(self, text: str, *, max_repeat: int) -> None:
        self.text = text
        self.pos = 0
        self.max_repeat = max_repeat

    # -- cursor helpers ----------------------------------------------------
    def _peek(self, ahead: int = 0) -> str | None:
        idx = self.pos + ahead
        return self.text[idx] if idx < len(self.text) else None

    def _next(self) -> str:
        ch = self.text[self.pos]
        self.pos += 1
        return ch

    def _fail(self, message: str) -> None:
        raise input_error(
            f"{message} (pattern {self.text!r}, position {self.pos})"
        )

    # -- entry point --------------------------------------------------------
    def parse(self) -> Node:
        node = self._alt()
        if self.pos != len(self.text):
            self._fail(f"unexpected {self._peek()!r}")
        return node

    # -- grammar ------------------------------------------------------------
    def _alt(self) -> Node:
        options = [self._concat()]
        while self._peek() == "|":
            self._next()
            options.append(self._concat())
        if len(options) == 1:
            return options[0]
        return Alt(tuple(options))

    def _concat(self) -> Node:
        parts: list[Node] = []
        while True:
            ch = self._peek()
            if ch is None or ch in "|)":
                break
            parts.append(self._repeat())
        if not parts:
            return Epsilon()
        if len(parts) == 1:
            return parts[0]
        return Concat(tuple(parts))

    def _repeat(self) -> Node:
        atom = self._atom()
        while True:
            ch = self._peek()
            if ch == "*":
                self._next()
                atom = Star(atom)
            elif ch == "+":
                self._next()
                atom = Plus(atom)
            elif ch == "?":
                self._next()
                atom = Opt(atom)
            elif ch == "{":
                atom = self._repeat_bounds(atom)
            else:
                return atom

    def _repeat_bounds(self, atom: Node) -> Node:
        self._next()  # consume '{'
        lo = self._digits()
        if lo is None:
            self._fail("malformed repeat: expected '{m}', '{m,}' or '{m,n}'")
        hi: int | None
        ch = self._peek()
        if ch == "}":
            self._next()
            hi = lo
        elif ch == ",":
            self._next()
            if self._peek() == "}":
                self._next()
                hi = None
            else:
                hi = self._digits()
                if hi is None:
                    self._fail("malformed repeat: expected upper bound or '}'")
                if self._peek() != "}":
                    self._fail("malformed repeat: expected '}'")
                self._next()
        else:
            self._fail("malformed repeat: expected ',' or '}'")
        assert lo is not None
        if hi is not None and hi < lo:
            self._fail(f"repeat range {{{lo},{hi}}} is empty")
        worst = hi if hi is not None else lo
        if worst > self.max_repeat:
            raise resource_exhausted(
                f"repeat count {worst} exceeds the configured limit "
                f"{self.max_repeat} (pattern {self.text!r})"
            )
        return Repeat(atom, lo, hi)

    def _digits(self) -> int | None:
        start = self.pos
        while (ch := self._peek()) is not None and ch.isdigit():
            self._next()
        if self.pos == start:
            return None
        return int(self.text[start : self.pos])

    def _atom(self) -> Node:
        ch = self._peek()
        if ch is None:
            self._fail("unexpected end of pattern")
        if ch == "(":
            self._next()
            node = self._alt()
            if self._peek() != ")":
                self._fail("unclosed group '('")
            self._next()
            return node
        if ch == "[":
            return self._class()
        if ch == ".":
            self._next()
            return Char(DOT_RANGES)
        if ch == "\\":
            self._next()
            return Char(self._escape_ranges())
        if ch in _META:
            self._fail(f"unexpected metacharacter {ch!r}")
        self._next()
        return Char(((ord(ch), ord(ch)),))

    # -- escapes and classes -------------------------------------------------
    def _escape_ranges(self) -> tuple[tuple[int, int], ...]:
        ch = self._peek()
        if ch is None:
            self._fail("trailing backslash")
        self._next()
        if ch in _CONTROL_ESCAPES:
            cp = _CONTROL_ESCAPES[ch]
            return ((cp, cp),)
        if ch in _CLASS_ESCAPES:
            return _CLASS_ESCAPES[ch]
        # Any other escaped character stands for itself (covers \. \\ \| ...).
        return ((ord(ch), ord(ch)),)

    def _class_item(self) -> tuple[tuple[int, int], ...]:
        ch = self._peek()
        if ch == "\\":
            self._next()
            return self._escape_ranges()
        self._next()
        return ((ord(ch), ord(ch)),)

    def _class(self) -> Node:
        self._next()  # consume '['
        negate = False
        if self._peek() == "^":
            negate = True
            self._next()
        ranges: list[tuple[int, int]] = []
        first = True
        while True:
            ch = self._peek()
            if ch is None:
                self._fail("unclosed character class '['")
            if ch == "]" and not first:
                self._next()
                break
            first = False
            item = self._class_item()
            # A 'x-y' range is only recognised when both endpoints are single
            # characters and '-' is not the last item before ']'.
            if (
                len(item) == 1
                and item[0][0] == item[0][1]
                and self._peek() == "-"
                and self._peek(1) not in (None, "]")
            ):
                self._next()  # consume '-'
                upper = self._class_item()
                if len(upper) != 1 or upper[0][0] != upper[0][1]:
                    self._fail("range endpoint must be a single character")
                lo, hi = item[0][0], upper[0][0]
                if hi < lo:
                    self._fail(
                        f"invalid range {chr(lo)!r}-{chr(hi)!r}: start > end"
                    )
                ranges.append((lo, hi))
            else:
                ranges.extend(item)
        if not ranges:
            self._fail("empty character class")
        normalized = normalize_ranges(tuple(ranges))
        if negate:
            normalized = complement_ranges(normalized)
        return Char(normalized)
