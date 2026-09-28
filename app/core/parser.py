"""Tokenizer and recursive-descent parser for the expression language.

The parser is intentionally strict: implicit multiplication is rejected, the
exponent of ``^`` must be a signed numeric literal, and unknown identifiers
are errors with an exact source span.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ast import (
    SUPPORTED_CONSTANTS,
    SUPPORTED_FUNCTIONS,
    VARIABLE_NAME,
    Binary,
    Call,
    Const,
    Node,
    Num,
    Paren,
    Unary,
    Var,
)
from .errors import ParseError

_TOKEN_NUM = "NUM"
_TOKEN_IDENT = "IDENT"
_TOKEN_LP = "("
_TOKEN_RP = ")"
_TOKEN_OP = "OP"
_TOKEN_EOF = "EOF"


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    start: int
    end: int


def _snippet(src: str, start: int, end: int) -> str:
    return src[start:end]


def tokenize(src: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            start = i
            seen_dot = False
            while i < n and (src[i].isdigit() or src[i] == "."):
                if src[i] == ".":
                    if seen_dot:
                        raise ParseError(
                            "number contains multiple decimal points",
                            start,
                            i + 1,
                            _snippet(src, start, i + 1),
                        )
                    seen_dot = True
                i += 1
            # scientific notation: e/E immediately followed by (digits) or
            # (+/- digits). A bare 'e' is the constant, never an exponent.
            if i < n and src[i] in ("e", "E"):
                j = i + 1
                if j < n and src[j] in ("+", "-"):
                    j += 1
                if j < n and src[j].isdigit():
                    i = j
                    while i < n and src[i].isdigit():
                        i += 1
                # otherwise: leave the 'e' for identifier tokenization
            end = i
            text = src[start:end]
            if text in (".", ""):
                raise ParseError("malformed number", start, max(end, start + 1), text)
            tokens.append(Token(_TOKEN_NUM, text, start, end))
            # A number directly glued to a letter is a typo (e.g. '2x').
            if i < n and (src[i].isalpha() or src[i] == "_"):
                raise ParseError(
                    "implicit multiplication is not allowed; write '*' explicitly",
                    start,
                    i + 1,
                    _snippet(src, start, i + 1),
                )
            continue
        if c.isalpha() or c == "_":
            start = i
            while i < n and (src[i].isalnum() or src[i] == "_"):
                i += 1
            tokens.append(Token(_TOKEN_IDENT, src[start:i], start, i))
            continue
        if c in "+-*/^()":
            kind = _TOKEN_LP if c == "(" else _TOKEN_RP if c == ")" else _TOKEN_OP
            tokens.append(Token(kind, c, i, i + 1))
            i += 1
            continue
        raise ParseError(f"unexpected character {c!r}", i, i + 1, c)
    tokens.append(Token(_TOKEN_EOF, "", n, n))
    return tokens


class Parser:
    def __init__(self, src: str) -> None:
        self.src = src
        self.tokens = tokenize(src)
        self.pos = 0

    # -- token helpers -----------------------------------------------------
    def _peek(self) -> Token:
        return self.tokens[self.pos]

    def _next(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _accept_op(self, op: str) -> Token | None:
        tok = self._peek()
        if tok.kind == _TOKEN_OP and tok.text == op:
            return self._next()
        return None

    def _error(self, message: str, tok: Token) -> ParseError:
        end = min(max(tok.end, tok.start + 1), len(self.src))
        start = min(tok.start, max(len(self.src) - 1, 0))
        if start >= end:
            start = max(end - 1, 0)
        return ParseError(message, start, end, self.src[start:end])

    # -- grammar -----------------------------------------------------------
    def parse(self) -> Node:
        if self._peek().kind == _TOKEN_EOF:
            raise ParseError("expression is empty", 0, 0, "")
        node = self._parse_expr()
        tok = self._peek()
        if tok.kind != _TOKEN_EOF:
            raise self._error(f"unexpected token {tok.text!r}", tok)
        return node

    def _parse_expr(self) -> Node:
        node = self._parse_term()
        while True:
            tok = self._peek()
            if tok.kind == _TOKEN_OP and tok.text in ("+", "-"):
                self._next()
                right = self._parse_term()
                node = Binary(
                    tok.text, node, right, start=node.start, end=right.end
                )
            else:
                return node

    def _parse_term(self) -> Node:
        node = self._parse_unary()
        while True:
            tok = self._peek()
            if tok.kind == _TOKEN_OP and tok.text in ("*", "/"):
                self._next()
                right = self._parse_unary()
                node = Binary(
                    tok.text, node, right, start=node.start, end=right.end
                )
            else:
                return node

    def _parse_unary(self) -> Node:
        tok = self._peek()
        if tok.kind == _TOKEN_OP and tok.text in ("+", "-"):
            self._next()
            operand = self._parse_unary()
            node: Node = Unary(
                tok.text, operand, start=tok.start, end=operand.end
            )
            if self._peek().kind == _TOKEN_OP and self._peek().text == "^":
                # -(...)^k should be read as -((...)^k); unary binds looser
                # than power, so fold the exponent onto the operand.
                self._next()
                exponent = self._parse_signed_number()
                inner = Binary(
                    "^", operand, exponent, start=operand.start, end=exponent.end
                )
                node = Unary(
                    tok.text, inner, start=tok.start, end=inner.end
                )
            return node
        return self._parse_power()

    def _parse_power(self) -> Node:
        base = self._parse_primary()
        if self._accept_op("^") is not None:
            exponent = self._parse_signed_number()
            return Binary(
                "^", base, exponent, start=base.start, end=exponent.end
            )
        return base

    def _parse_signed_number(self) -> Node:
        """The exponent of '^' must be a signed numeric literal."""
        tok = self._peek()
        sign_tok: Token | None = None
        if tok.kind == _TOKEN_OP and tok.text in ("+", "-"):
            sign_tok = tok
            self._next()
            tok = self._peek()
        if tok.kind != _TOKEN_NUM:
            where = sign_tok or tok
            raise self._error(
                "exponent of '^' must be a numeric literal (e.g. '^2', '^-1')",
                where,
            )
        self._next()
        num: Node = Num(tok.text, start=tok.start, end=tok.end)
        if sign_tok is not None and sign_tok.text == "-":
            num = Unary("-", num, start=sign_tok.start, end=tok.end)
        return num

    def _parse_primary(self) -> Node:
        tok = self._peek()
        if tok.kind == _TOKEN_NUM:
            self._next()
            return Num(tok.text, start=tok.start, end=tok.end)
        if tok.kind == _TOKEN_IDENT:
            self._next()
            if tok.text in SUPPORTED_FUNCTIONS:
                lp = self._peek()
                if lp.kind != _TOKEN_LP:
                    raise self._error(f"expected '(' after function {tok.text!r}", lp)
                self._next()
                arg = self._parse_expr()
                rp = self._peek()
                if rp.kind != _TOKEN_RP:
                    raise self._error(f"missing ')' for function {tok.text!r}", rp)
                self._next()
                return Call(tok.text, arg, start=tok.start, end=rp.end)
            if tok.text == VARIABLE_NAME:
                return Var(start=tok.start, end=tok.end)
            if tok.text in SUPPORTED_CONSTANTS:
                return Const(tok.text, start=tok.start, end=tok.end)
            raise ParseError(
                f"unknown identifier {tok.text!r}; allowed: x, pi, e, "
                f"{', '.join(sorted(SUPPORTED_FUNCTIONS))}",
                tok.start,
                tok.end,
                tok.text,
            )
        if tok.kind == _TOKEN_LP:
            self._next()
            inner = self._parse_expr()
            rp = self._peek()
            if rp.kind != _TOKEN_RP:
                raise self._error("missing closing ')'", rp)
            self._next()
            # Parentheses are semantically transparent; keep the wrapper only
            # to preserve source spans for error reporting.
            return Paren(inner, start=tok.start, end=rp.end)
        if tok.kind == _TOKEN_EOF:
            raise self._error("unexpected end of expression", tok)
        raise self._error(f"unexpected token {tok.text!r}", tok)


def parse_expression(src: str) -> Node:
    """Parse ``src`` into an AST, raising :class:`ParseError` on failure."""
    parser = Parser(src)
    return parser.parse()
