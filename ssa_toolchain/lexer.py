"""Hand-written lexer for the mini language and for IR text.

The mini language is tokenized with this keyword/punctuation table; the IR
text format (see :mod:`ssa_toolchain.ir_parser`) reuses the machinery with a
different keyword set.
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import LexError, Loc


@dataclass(frozen=True)
class Token:
    kind: str       # identifier / integer / keyword name / punct spelling
    value: object   # int for integers, str otherwise
    loc: Loc

    def __str__(self) -> str:
        return repr(self.value) if self.kind in ("identifier", "integer") else str(self.value)


class Lexer:
    def __init__(self, source: str, file: str = "<input>", keywords=(),
                 multi_char=(), single_chars=""):
        self.s = source
        self.file = file
        self.n = len(source)
        self.i = 0
        self.line = 1
        self.col = 1
        self.keywords = set(keywords)
        self.multi = sorted(multi_char, key=len, reverse=True)
        self.single = set(single_chars)

    # -- low level ----------------------------------------------------------

    def _loc_from(self, offset, line, col, end_off, end_line=None, end_col=None):
        return Loc(
            file=self.file,
            offset=offset,
            end_offset=end_off,
            line=line,
            col=col,
            end_line=end_line or line,
            end_col=end_col or col,
        )

    def _skip_ws_comments(self):
        while self.i < self.n:
            c = self.s[self.i]
            if c in " \t\r\n":
                self._advance()
            elif c == "/" and self.i + 1 < self.n and self.s[self.i + 1] == "/":
                while self.i < self.n and self.s[self.i] != "\n":
                    self._advance()
            elif c == "/" and self.i + 1 < self.n and self.s[self.i + 1] == "*":
                start_line, start_col = self.line, self.col
                start = self.i
                self._advance(); self._advance()
                ok = False
                while self.i < self.n:
                    if self.s[self.i] == "*" and self.i + 1 < self.n and self.s[self.i + 1] == "/":
                        self._advance(); self._advance()
                        ok = True
                        break
                    self._advance()
                if not ok:
                    raise LexError("unterminated block comment",
                                   self._loc_from(start, start_line, start_col, self.n,
                                                  self.line, self.col))
            else:
                break

    def _advance(self):
        c = self.s[self.i]
        self.i += 1
        if c == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1

    # -- token level --------------------------------------------------------

    def tokenize(self) -> list[Token]:
        out: list[Token] = []
        while True:
            self._skip_ws_comments()
            if self.i >= self.n:
                out.append(Token("eof", "", Loc(self.file, self.n, self.n,
                                                self.line, self.col, self.line, self.col)))
                return out
            tok = self._one()
            out.append(tok)

    def _one(self) -> Token:
        start, line, col = self.i, self.line, self.col
        c = self.s[self.i]

        if c.isalpha() or c == "_":
            while self.i < self.n and (self.s[self.i].isalnum() or self.s[self.i] == "_"):
                self._advance()
            text = self.s[start:self.i]
            kind = text if text in self.keywords else "identifier"
            return Token(kind, text, self._loc(start, line, col))

        if c.isdigit():
            base = 10
            if c == "0" and self.i + 1 < self.n and self.s[self.i + 1] in "xX":
                self._advance(); self._advance()
                base = 16
            while self.i < self.n:
                ch = self.s[self.i]
                if ch.isdigit() or (base == 16 and ch in "abcdefABCDEF"):
                    self._advance()
                else:
                    break
            text = self.s[start:self.i]
            try:
                val = int(text, base)
            except ValueError:
                raise LexError(f"bad integer literal {text!r}", self._loc(start, line, col))
            return Token("integer", val, self._loc(start, line, col))

        for op in self.multi:
            if self.s.startswith(op, self.i):
                for _ in op:
                    self._advance()
                return Token(op, op, self._loc(start, line, col))

        if c in self.single:
            self._advance()
            return Token(c, c, self._loc(start, line, col))

        raise LexError(f"unexpected character {c!r}", self._loc(start, line, col))

    def _loc(self, start, line, col):
        return Loc(self.file, start, self.i, line, col, self.line, self.col)


MINI_KEYWORDS = (
    "func", "var", "if", "else", "while", "return", "true", "false",
)
MINI_MULTI = (
    "==", "!=", "<=", ">=", "&&", "||", "<<", ">>",
)
MINI_SINGLE = "(){};,+-*/%<>=!&|^~"


def tokenize_mini(source: str, file: str = "<input>") -> list[Token]:
    return Lexer(source, file, MINI_KEYWORDS, MINI_MULTI, MINI_SINGLE).tokenize()
