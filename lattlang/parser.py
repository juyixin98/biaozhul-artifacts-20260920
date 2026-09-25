"""Hand-written recursive-descent parser for LattLang.

Takes tokens from :mod:`lattlang.lexer` and builds the AST in
:mod:`lattlang.ast_nodes`; every node carries a source :class:`Span`.
Semicolons after ``:=`` / ``print`` statements are optional.
"""

from __future__ import annotations

from . import ast_nodes as ast
from .errors import LangError, Span
from .lexer import Token, tokenize

# Binary operators grouped by precedence level (lowest first).
_BIN_PRECEDENCE: list[tuple[str, ...]] = [
    ("||",),
    ("&&",),
    ("==", "!="),
    ("<", "<=", ">", ">="),
    ("+", "-"),
    ("*", "/", "%"),
]


class Parser:
    def __init__(self, tokens: list[Token]):
        self.toks = tokens
        self.pos = 0

    # -- token cursor ------------------------------------------------------

    @property
    def cur(self) -> Token:
        return self.toks[self.pos]

    def _advance(self) -> Token:
        tok = self.toks[self.pos]
        if tok.kind != "EOF":
            self.pos += 1
        return tok

    def _expect(self, kind: str) -> Token:
        tok = self.cur
        if tok.kind != kind:
            raise LangError(f"expected {kind!r} but found {tok.text!r}", "parse", tok.span)
        return self._advance()

    def _accept(self, kind: str) -> Token | None:
        if self.cur.kind == kind:
            return self._advance()
        return None

    # -- programs / statements --------------------------------------------

    def parse_program(self) -> ast.Program:
        start = self.cur.span
        body: list[ast.Stmt] = []
        while self.cur.kind != "EOF":
            body.append(self.parse_stmt())
        if body:
            span = body[0].span.merge(body[-1].span)
        else:
            span = Span(start.start_line, start.start_col, start.end_line,
                        start.end_col, start.offset, 0)
        return ast.Program(body, span)

    def parse_stmt(self) -> ast.Stmt:
        kind = self.cur.kind
        if kind == "IDENT":
            return self.parse_assign()
        if kind == "print":
            return self.parse_print()
        if kind == "if":
            return self.parse_if()
        if kind == "while":
            return self.parse_while()
        raise LangError(f"expected a statement but found {self.cur.text!r}",
                        "parse", self.cur.span)

    def parse_block(self) -> list[ast.Stmt]:
        self._expect("{")
        stmts: list[ast.Stmt] = []
        while self.cur.kind != "}":
            if self.cur.kind == "EOF":
                raise LangError("unterminated block: expected '}'", "parse",
                                self.cur.span)
            stmts.append(self.parse_stmt())
        self._expect("}")
        return stmts

    def parse_assign(self) -> ast.Assign:
        target = self._expect("IDENT")
        self._expect(":=")
        value = self.parse_expr()
        self._accept(";")
        return ast.Assign(
            target=target.text,
            target_span=target.span,
            value=value,
            span=target.span.merge(_expr_end(value)),
        )

    def parse_print(self) -> ast.Print:
        kw = self._expect("print")
        value = self.parse_expr()
        self._accept(";")
        return ast.Print(value, kw.span.merge(_expr_end(value)))

    def parse_if(self) -> ast.If:
        kw = self._expect("if")
        cond = self.parse_expr()
        then_body = self.parse_block()
        else_body: list[ast.Stmt] = []
        if self._accept("else") is not None:
            if self.cur.kind == "if":
                else_body = [self.parse_if()]
            else:
                else_body = self.parse_block()
        end_span = (else_body[-1].span if else_body
                    else then_body[-1].span if then_body else cond.span)
        return ast.If(cond, then_body, else_body, kw.span.merge(end_span))

    def parse_while(self) -> ast.While:
        kw = self._expect("while")
        cond = self.parse_expr()
        body = self.parse_block()
        end_span = body[-1].span if body else cond.span
        return ast.While(cond, body, kw.span.merge(end_span))

    # -- expressions -------------------------------------------------------

    def parse_expr(self) -> ast.Expr:
        return self._parse_binary(0)

    def _parse_binary(self, level: int) -> ast.Expr:
        if level >= len(_BIN_PRECEDENCE):
            return self._parse_unary()
        left = self._parse_binary(level + 1)
        while self.cur.kind in _BIN_PRECEDENCE[level]:
            op_tok = self._advance()
            right = self._parse_binary(level + 1)
            left = ast.Binary(op_tok.kind, left, right,
                              left.span.merge(_expr_end(right)))
        return left

    def _parse_unary(self) -> ast.Expr:
        if self.cur.kind in ("-", "!"):
            op_tok = self._advance()
            value = self._parse_unary()
            return ast.Unary(op_tok.kind, value, op_tok.span.merge(_expr_end(value)))
        return self._parse_primary()

    def _parse_primary(self) -> ast.Expr:
        tok = self.cur
        if tok.kind == "INT":
            self._advance()
            return ast.IntLit(int(tok.value), tok.span)
        if tok.kind in ("true", "false"):
            self._advance()
            return ast.BoolLit(tok.kind == "true", tok.span)
        if tok.kind == "IDENT":
            self._advance()
            return ast.Var(tok.text, tok.span)
        if tok.kind == "(":
            self._advance()
            expr = self.parse_expr()
            close = self._expect(")")
            return expr
        raise LangError(f"expected an expression but found {tok.text!r}",
                        "parse", tok.span)


def _expr_end(e: ast.Expr) -> Span:
    return e.span


def parse(source: str) -> ast.Program:
    return Parser(tokenize(source)).parse_program()
