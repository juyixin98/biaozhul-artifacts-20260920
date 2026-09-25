"""Hand-written lexer for LattLang.

Produces tokens with precise :class:`~lattlang.errors.Span` positions; nothing
is delegated to an external compiler or parser generator.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import LangError, Span

KEYWORDS = {"if", "else", "while", "print", "true", "false"}

# Multi-character operators first so ``<=`` wins over ``<``.
_OPERATORS = ["==", "!=", "<=", ">=", "&&", "||", ":=", "<", ">", "!",
              "(", ")", "{", "}", ";", "+", "-", "*", "/", "%"]


@dataclass(frozen=True)
class Token:
    kind: str          # "INT" | "IDENT" | keyword text | operator text | "EOF"
    text: str
    value: int | str | None
    span: Span


class Lexer:
    def __init__(self, source: str):
        self.src = source
        self.n = len(source)
        self.i = 0
        self.line = 1
        self.col = 1
        self.tokens: list[Token] = []

    # -- position helpers --------------------------------------------------

    def _pos(self) -> tuple[int, int, int]:
        return self.line, self.col, self.i

    def _set_pos(self, pos: tuple[int, int, int]) -> None:
        self.line, self.col, self.i = pos

    def _advance(self) -> str:
        ch = self.src[self.i]
        self.i += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def _make_span(self, start: tuple[int, int, int], end_off: int) -> Span:
        sl, sc, so = start
        # End position is the cursor after the token; represent it with the
        # same line/col convention (1-based, one past the last char).
        return Span(sl, sc, self.line, self.col, so, end_off - so)

    # -- entry point -------------------------------------------------------

    def tokenize(self) -> list[Token]:
        while self.i < self.n:
            ch = self.src[self.i]
            if ch in " \t\r\n":
                self._advance()
                continue
            if ch == "#":  # line comments, to end of line
                while self.i < self.n and self.src[self.i] != "\n":
                    self._advance()
                continue
            start = self._pos()
            if ch.isdigit():
                self._number(start)
            elif ch.isalpha() or ch == "_":
                self._ident(start)
            else:
                self._operator(start)
        eof_pos = self._pos()
        self.tokens.append(Token("EOF", "", None,
                                 Span(self.line, self.col, self.line, self.col,
                                      self.n, 0)))
        return self.tokens

    # -- token scanners ----------------------------------------------------

    def _number(self, start: tuple[int, int, int]) -> None:
        while self.i < self.n and self.src[self.i].isdigit():
            self._advance()
        text = self.src[start[2]:self.i]
        self.tokens.append(Token("INT", text, int(text), self._make_span(start, self.i)))

    def _ident(self, start: tuple[int, int, int]) -> None:
        while self.i < self.n and (self.src[self.i].isalnum() or self.src[self.i] == "_"):
            self._advance()
        text = self.src[start[2]:self.i]
        kind = text if text in KEYWORDS else "IDENT"
        self.tokens.append(Token(kind, text, text, self._make_span(start, self.i)))

    def _operator(self, start: tuple[int, int, int]) -> None:
        for op in _OPERATORS:
            if self.src.startswith(op, self.i):
                for _ in op:
                    self._advance()
                self.tokens.append(Token(op, op, op, self._make_span(start, self.i)))
                return
        raise LangError(f"unexpected character {self.src[self.i]!r}", "lex",
                        self._make_span(start, self.i + 1))


def tokenize(source: str) -> list[Token]:
    return Lexer(source).tokenize()
