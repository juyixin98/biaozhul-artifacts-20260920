"""Hand-written lexer for Slang.

No regular-expression library or parser generator is used: the source is
scanned character by character and every produced token carries a full
:class:`~slang.errors.Loc` span (line/column plus absolute offset).
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import LexError, Loc

KEYWORDS = {
    "let",
    "fn",
    "return",
    "if",
    "else",
    "while",
    "true",
    "false",
    "null",
}

# Multi-character operators, longest first so "<=" is preferred over "<".
TWO_CHAR_OPS = ("==", "!=", "<=", ">=", "&&", "||")
ONE_CHAR_OPS = set("+-*/%<>=!(){}[],;")


@dataclass(frozen=True)
class Token:
    kind: str          # "ID", "INT", "STR", "KEYWORD:<word>", or the punctuator itself
    value: object      # int for INT, str for STR/ID, None for punctuators
    text: str
    loc: Loc


class Lexer:
    def __init__(self, source: str, filename: str = "<input>"):
        self.src = source
        self.filename = filename
        self.n = len(source)
        self.i = 0
        self.line = 1
        self.col = 1
        self.tokens: list[Token] = []

    # -- low-level cursor helpers ------------------------------------------

    def _peek(self, off: int = 0) -> str:
        j = self.i + off
        return self.src[j] if j < self.n else ""

    def _advance(self) -> str:
        ch = self.src[self.i]
        self.i += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def _loc_from(self, line: int, col: int, offset: int) -> Loc:
        return Loc(line, col, self.line, self.col, offset, self.i)

    # -- entry point --------------------------------------------------------

    def tokenize(self) -> list[Token]:
        while self.i < self.n:
            ch = self._peek()
            if ch in " \t\r\n":
                self._advance()
            elif ch == "/" and self._peek(1) == "/":
                self._line_comment()
            elif ch == "/" and self._peek(1) == "*":
                self._block_comment()
            elif ch.isalpha() or ch == "_":
                self._identifier()
            elif ch.isdigit():
                self._number()
            elif ch == '"':
                self._string()
            elif ch == "'":
                self._string(single_quote=True)
            elif self._starts_two_char_op():
                self._punctuator(length=2)
            elif ch in ONE_CHAR_OPS:
                self._punctuator(length=1)
            else:
                raise LexError(f"unexpected character {ch!r}", self._loc_from(self.line, self.col, self.i))
        self.tokens.append(Token("EOF", None, "", Loc(self.line, self.col, self.line, self.col, self.n, self.n)))
        return self.tokens

    def _starts_two_char_op(self) -> bool:
        return self.i + 1 < self.n and self.src[self.i : self.i + 2] in TWO_CHAR_OPS

    def _line_comment(self):
        while self.i < self.n and self._peek() != "\n":
            self._advance()

    def _block_comment(self):
        start_line, start_col, start_off = self.line, self.col, self.i
        self._advance()
        self._advance()  # opening /*
        depth = 1
        while self.i < self.n and depth > 0:
            if self._peek() == "/" and self._peek(1) == "*":
                self._advance()
                self._advance()
                depth += 1
            elif self._peek() == "*" and self._peek(1) == "/":
                self._advance()
                self._advance()
                depth -= 1
            else:
                self._advance()
        if depth > 0:
            raise LexError("unterminated block comment", self._loc_from(start_line, start_col, start_off))

    def _identifier(self):
        start_line, start_col, start_off = self.line, self.col, self.i
        start = self.i
        while self.i < self.n and (self._peek().isalnum() or self._peek() == "_"):
            self._advance()
        text = self.src[start : self.i]
        kind = f"KEYWORD:{text}" if text in KEYWORDS else "ID"
        self.tokens.append(Token(kind, text, text, self._loc_from(start_line, start_col, start_off)))

    def _number(self):
        start_line, start_col, start_off = self.line, self.col, self.i
        start = self.i
        while self.i < self.n and self._peek().isdigit():
            self._advance()
        text = self.src[start : self.i]
        self.tokens.append(Token("INT", int(text), text, self._loc_from(start_line, start_col, start_off)))

    def _string(self, single_quote: bool = False):
        quote = "'" if single_quote else '"'
        start_line, start_col, start_off = self.line, self.col, self.i
        start = self.i
        self._advance()  # opening quote
        chars: list[str] = []
        escapes = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "0": "\0"}
        while self.i < self.n and self._peek() != quote:
            ch = self._advance()
            if ch == "\n":
                raise LexError("unterminated string literal", self._loc_from(start_line, start_col, start_off))
            if ch == "\\":
                if self.i >= self.n:
                    raise LexError("unterminated string literal", self._loc_from(start_line, start_col, start_off))
                esc = self._advance()
                if esc not in escapes:
                    raise LexError(f"invalid string escape \\{esc}", self._loc_from(start_line, start_col, start_off))
                chars.append(escapes[esc])
            else:
                chars.append(ch)
        if self.i >= self.n:
            raise LexError("unterminated string literal", self._loc_from(start_line, start_col, start_off))
        self._advance()  # closing quote
        text = "".join(chars)
        self.tokens.append(Token("STR", text, self.src[start : self.i], self._loc_from(start_line, start_col, start_off)))

    def _punctuator(self, length: int):
        start_line, start_col, start_off = self.line, self.col, self.i
        text = self.src[self.i : self.i + length]
        for _ in range(length):
            self._advance()
        self.tokens.append(Token(text, None, text, self._loc_from(start_line, start_col, start_off)))


def tokenize(source: str, filename: str = "<input>") -> list[Token]:
    return Lexer(source, filename).tokenize()
