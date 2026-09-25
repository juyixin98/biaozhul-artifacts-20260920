"""Hand-written lexer for MiniML.

Produces tokens with exact source positions. Comments are ``(* ... *)`` and
nest. No token is silently dropped without its location.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .span import Pos, Span


KEYWORDS = {
    "let",
    "rec",
    "in",
    "fun",
    "if",
    "then",
    "else",
    "true",
    "false",
    "ref",
    "unit",
}

# Multi-character operators, longest first so the scanner prefers them.
MULTI_OPS = ("<=", ">=", "<>", ":=", "->", "&&", "||")
SINGLE_OPS = set("+-*/=<>!();,")


class LexError(Exception):
    def __init__(self, message: str, pos: Pos, end_pos: Optional[Pos] = None):
        super().__init__(message)
        self.message = message
        self.pos = pos
        self.end_pos = end_pos or Pos(pos.offset + 1, pos.line, pos.col + 1)

    @property
    def span(self) -> Span:
        return Span(self.pos, self.end_pos)


@dataclass(frozen=True)
class Token:
    kind: str  # KEYWORD token kind is the keyword itself, e.g. 'let'; ops too
    text: str
    span: Span

    def __str__(self) -> str:
        return f"{self.kind}({self.text!r})@{self.span.start}"


class _Scanner:
    def __init__(self, src: str):
        self.src = src
        self.n = len(src)
        self.i = 0
        self.line = 1
        self.col = 1

    def pos(self) -> Pos:
        return Pos(self.i, self.line, self.col)

    def peek(self, off: int = 0) -> str:
        j = self.i + off
        return self.src[j] if j < self.n else ""

    def advance(self) -> str:
        c = self.src[self.i]
        self.i += 1
        if c == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return c

    def skip_trivia(self) -> None:
        while self.i < self.n:
            c = self.peek()
            if c in " \t\r\n":
                self.advance()
            elif c == "(" and self.peek(1) == "*":
                self.scan_comment()
            else:
                break

    def scan_comment(self) -> None:
        start = self.pos()
        depth = 0
        while self.i < self.n:
            if self.peek() == "(" and self.peek(1) == "*":
                self.advance()
                self.advance()
                depth += 1
            elif self.peek() == "*" and self.peek(1) == ")":
                self.advance()
                self.advance()
                depth -= 1
                if depth == 0:
                    return
            else:
                self.advance()
        raise LexError("unterminated comment, expected '*)'", start)

    def scan_token(self) -> Token:
        self.skip_trivia()
        if self.i >= self.n:
            p = self.pos()
            return Token("EOF", "", Span(p, p))

        start = self.pos()
        c = self.peek()

        if c.isalpha() or c == "_":
            return self.scan_ident(start)
        if c.isdigit():
            return self.scan_number(start)
        return self.scan_operator(start)

    def scan_ident(self, start: Pos) -> Token:
        while self.i < self.n and (self.peek().isalnum() or self.peek() == "_"):
            self.advance()
        end = self.pos()
        text = self.src[start.offset : end.offset]
        kind = text if text in KEYWORDS else "IDENT"
        return Token(kind, text, Span(start, end))

    def scan_number(self, start: Pos) -> Token:
        while self.i < self.n and self.peek().isdigit():
            self.advance()
        # Reject identifiers starting with a digit, e.g. 123abc.
        if self.i < self.n and (self.peek().isalpha() or self.peek() == "_"):
            bad_start = self.pos()
            while self.i < self.n and (self.peek().isalnum() or self.peek() == "_"):
                self.advance()
            bad_end = self.pos()
            raise LexError(
                "number may not be immediately followed by letters",
                bad_start,
                bad_end,
            )
        end = self.pos()
        text = self.src[start.offset : end.offset]
        return Token("INT", text, Span(start, end))

    def scan_operator(self, start: Pos) -> Token:
        two = self.src[self.i : self.i + 2]
        if two in MULTI_OPS:
            self.advance()
            self.advance()
            kind = two  # operators are their own token kind
        elif c := self.peek():
            if c in SINGLE_OPS:
                self.advance()
                kind = c
            else:
                self.advance()
                raise LexError(f"unexpected character {c!r}", start)
        else:  # pragma: no cover - guarded by caller
            raise LexError("unexpected end of input", start)
        end = self.pos()
        return Token(kind, self.src[start.offset : end.offset], Span(start, end))


def lex(src: str) -> list[Token]:
    """Lex *src* into tokens, including a trailing ``EOF`` token."""
    sc = _Scanner(src)
    tokens: list[Token] = []
    while True:
        tok = sc.scan_token()
        tokens.append(tok)
        if tok.kind == "EOF":
            return tokens
