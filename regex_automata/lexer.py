"""Hand-written lexer for the documented regex subset.

Every emitted token carries its source :class:`~regex_automata.locations.Span`.
Nothing in here uses Python's ``re`` module — the lexer is a plain
recursive character scanner.
"""
from __future__ import annotations

from .errors import LexError
from .locations import Span
from .predicates import CharPredicate, CharSet, Not, combine_or
from .tokens import (
    T_ANCHOR,
    T_CHAR,
    T_CLASS,
    T_DOT,
    T_EOF,
    T_LPAREN,
    T_PIPE,
    T_QUANT,
    T_RPAREN,
    Token,
)

_MAX_REPEAT = 1000  # bounded quantifier expansion ceiling (see docs/language.md)

# Characters that are always meta outside character classes.
_META = set(".^$()|*+?{}\\[]")

# Fixed simple escapes; exact semantics are documented in docs/language.md.
_SIMPLE_ESCAPES = {
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "f": 0x0C,
    "v": 0x0B,
    "0": 0x00,
    "a": 0x07,
    "e": 0x1B,
}

# Shorthand character-class escapes: name -> negated
_SHORTHAND = {
    "d": ("digit", False),
    "D": ("digit", True),
    "w": ("word", False),
    "W": ("word", True),
    "s": ("space", False),
    "S": ("space", True),
}

# Escapes that keep their literal meaning (punctuation escaping).
_PUNCT_ESCAPES = set(".^$()|*+?{}\\[]") | {"/", "-", "#", " "}


class Lexer:
    def __init__(self, source: str) -> None:
        self.s = source
        self.i = 0
        self.n = len(source)

    # helpers ------------------------------------------------------------------
    def _error(self, message: str, start: int, end: int | None = None) -> LexError:
        return LexError(message, Span(start, self.n if end is None else end), self.s)

    def read_escape(self) -> tuple[int | None, CharPredicate | None, str | None, int]:
        """Read ``\\X`` at ``self.i`` (the backslash).

        Returns ``(cp, predicate, anchor, end_offset)`` where exactly one of
        ``cp`` / ``predicate`` / ``anchor`` is not ``None``.
        """
        start = self.i
        assert self.s[self.i] == "\\"
        self.i += 1
        if self.i >= self.n:
            raise self._error("dangling escape: pattern ends with '\\'", start)
        ch = self.s[self.i]

        # simple one-char escapes
        if ch in _SIMPLE_ESCAPES:
            self.i += 1
            return _SIMPLE_ESCAPES[ch], None, None, self.i

        # punctuation / other literal escapes
        if ch in _PUNCT_ESCAPES:
            self.i += 1
            return ord(ch), None, None, self.i

        # shorthand character classes
        if ch in _SHORTHAND:
            self.i += 1
            name, neg = _SHORTHAND[ch]
            return None, CharPredicate.named(name, neg), None, self.i

        # word-boundary anchors
        if ch == "b":
            self.i += 1
            return None, None, "b", self.i
        if ch == "B":
            self.i += 1
            return None, None, "B", self.i

        # fixed-width numeric escapes
        if ch == "x":
            cp = self._read_hex(2, start)
            return cp, None, None, self.i
        if ch == "u":
            cp = self._read_hex(4, start)
            return cp, None, None, self.i

        raise self._error(
            f"unsupported escape sequence '\\{ch}' (this engine supports no "
            "backreferences, octal beyond \\0, \\cX, \\p{...} or \\x{{...}})",
            start,
            self.i + 1,
        )

    def _read_hex(self, digits: int, esc_start: int) -> int:
        # self.s[self.i] is the 'x'/'u' marker
        self.i += 1
        hex_start = self.i
        text = ""
        for _ in range(digits):
            if self.i >= self.n:
                raise self._error(
                    f"incomplete hex escape: expected {digits} hexadecimal digits",
                    esc_start,
                    self.n,
                )
            c = self.s[self.i]
            if c not in "0123456789abcdefABCDEF":
                raise self._error(
                    f"invalid hexadecimal digit {c!r} in escape",
                    hex_start,
                    self.i + 1,
                )
            text += c
            self.i += 1
        return int(text, 16)

    # quantifiers --------------------------------------------------------------
    def try_read_quantifier(self) -> Token | None:
        """If a ``{m}`` / ``{m,}`` / ``{m,n}`` starts here, consume and return.

        Otherwise leave the scanner untouched and return ``None`` (the ``{``
        is then treated as an ordinary literal character).
        """
        start = self.i
        j = start + 1
        m_digits = ""
        while j < self.n and self.s[j].isdigit():
            m_digits += self.s[j]
            j += 1
        if not m_digits:
            return None
        if j >= self.n:
            return None
        if self.s[j] == "}":
            j += 1
            self.i = j
            return self._make_quant(start, j, int(m_digits), int(m_digits))
        if self.s[j] != ",":
            return None
        j += 1
        n_digits = ""
        while j < self.n and self.s[j].isdigit():
            n_digits += self.s[j]
            j += 1
        if j >= self.n or self.s[j] != "}":
            return None
        j += 1
        minimum = int(m_digits)
        maximum = int(n_digits) if n_digits else None
        if maximum is not None and maximum < minimum:
            raise self._error(
                f"quantifier range out of order: {{{m_digits},"
                f"{n_digits}}} requires min <= max",
                start,
                j,
            )
        self.i = j
        return self._make_quant(start, j, minimum, maximum)

    def _make_quant(self, start: int, end: int, lo: int, hi: int | None) -> Token:
        biggest = hi if hi is not None else lo
        if biggest > _MAX_REPEAT:
            raise self._error(
                f"repeat count {biggest} exceeds the supported maximum of "
                f"{_MAX_REPEAT} (finite repetitions are Thompson-expanded)",
                start,
                end,
            )
        return Token(T_QUANT, Span(start, end), minimum=lo, maximum=hi)

    # character classes --------------------------------------------------------
    def read_class(self) -> Token:
        """Consume a complete ``[...]`` starting at ``self.i``."""
        start = self.i
        assert self.s[self.i] == "["
        self.i += 1
        negated = False
        if self.i < self.n and self.s[self.i] == "^":
            negated = True
            self.i += 1
        items: list[CharPredicate] = []
        # A ] or - immediately after the opening '[' / '[^' is a literal.
        first = True
        while True:
            if self.i >= self.n:
                raise self._error("unterminated character class", start, self.n)
            ch = self.s[self.i]
            if ch == "]" and not first:
                self.i += 1
                break
            first = False

            item_start = self.i
            cp: int | None
            if ch == "\\":
                ecp, pred, anchor, _ = self.read_escape()
                if anchor is not None:
                    raise self._error(
                        f"escape '\\{anchor}' is an anchor and may not appear "
                        "inside a character class",
                        item_start,
                        self.i,
                    )
                if pred is not None:
                    items.append(pred)
                    continue
                cp = ecp  # the escape produced a literal code point
            elif ch == "[" and self.i + 1 < self.n and self.s[self.i + 1] == "[":
                raise self._error(
                    "POSIX classes and nested '[' are not supported",
                    self.i,
                    self.i + 2,
                )
            else:
                cp = ord(ch)
                self.i += 1

            # range?
            if self.i < self.n and self.s[self.i] == "-" and self.i + 1 < self.n and (
                self.s[self.i + 1] != "]"
            ):
                self.i += 1  # consume '-'
                hi_ch = self.s[self.i]
                hi_cp: int
                if hi_ch == "\\":
                    hcp, hpred, hanchor, _ = self.read_escape()
                    if hanchor is not None or hpred is not None:
                        raise self._error(
                            "range endpoint must be a literal character or "
                            "simple character escape",
                            item_start,
                            self.i,
                        )
                    hi_cp = hcp  # type: ignore[assignment]
                else:
                    hi_cp = ord(hi_ch)
                    self.i += 1
                if hi_cp < cp:
                    raise self._error(
                        "character range is reversed: end code point precedes "
                        "start code point",
                        item_start,
                        self.i,
                    )
                items.append(CharSet(((cp, hi_cp),), ()))
            else:
                items.append(CharPredicate.literal(cp))

        positive = combine_or(items)
        if negated:
            # [^...] = complement of the positive set over the unrestricted
            # code-point domain (Not covers every code point outside the set).
            predicate: CharPredicate = Not(positive)
        else:
            predicate = positive
        return Token(T_CLASS, Span(start, self.i), predicate=predicate)

    # main loop ----------------------------------------------------------------
    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while self.i < self.n:
            ch = self.s[self.i]
            start = self.i

            if ch == ".":
                self.i += 1
                tokens.append(Token(T_DOT, Span(start, self.i)))
            elif ch == "(":
                self.i += 1
                tokens.append(Token(T_LPAREN, Span(start, self.i)))
            elif ch == ")":
                self.i += 1
                tokens.append(Token(T_RPAREN, Span(start, self.i)))
            elif ch == "|":
                self.i += 1
                tokens.append(Token(T_PIPE, Span(start, self.i)))
            elif ch == "*":
                self.i += 1
                tokens.append(
                    Token(T_QUANT, Span(start, self.i), minimum=0, maximum=None)
                )
            elif ch == "+":
                self.i += 1
                tokens.append(
                    Token(T_QUANT, Span(start, self.i), minimum=1, maximum=None)
                )
            elif ch == "?":
                self.i += 1
                tokens.append(
                    Token(T_QUANT, Span(start, self.i), minimum=0, maximum=1)
                )
            elif ch == "^":
                self.i += 1
                tokens.append(Token(T_ANCHOR, Span(start, self.i), anchor="^"))
            elif ch == "$":
                self.i += 1
                tokens.append(Token(T_ANCHOR, Span(start, self.i), anchor="$"))
            elif ch == "[":
                tokens.append(self.read_class())
            elif ch == "{":
                quant = self.try_read_quantifier()
                if quant is not None:
                    tokens.append(quant)
                else:
                    # bare '{' is an ordinary literal (documented behaviour)
                    self.i += 1
                    tokens.append(Token(T_CHAR, Span(start, self.i), cp=ord("{")))
            elif ch == "}":
                self.i += 1
                tokens.append(Token(T_CHAR, Span(start, self.i), cp=ord("}")))
            elif ch == "]":
                raise self._error("unbalanced ']' with no matching '['", start, start + 1)
            elif ch == "\\":
                cp, pred, anchor, end = self.read_escape()
                span = Span(start, end)
                if pred is not None:
                    tokens.append(Token(T_CLASS, span, predicate=pred))
                elif anchor is not None:
                    tokens.append(Token(T_ANCHOR, span, anchor=anchor))
                else:
                    tokens.append(Token(T_CHAR, span, cp=cp))
            else:
                self.i += 1
                tokens.append(Token(T_CHAR, Span(start, self.i), cp=ord(ch)))

        tokens.append(Token(T_EOF, Span(self.n, self.n)))
        return tokens


def lex(source: str) -> list[Token]:
    return Lexer(source).tokenize()
