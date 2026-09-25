"""Hand-written parser for MiniML (recursive descent + precedence climbing).

Grammar (EBNF-ish, see README.md for the documented version)::

    program := item* final-expr?
    item    := toplet (';;' | EOF)
    toplet  := 'let' 'rec'? IDENT '=' expr
    expr    := let-expr | fun-expr | if-expr | seq-expr
    seq-expr:= or-expr (';' or-expr)*
    or-expr := and-expr ('||' and-expr)*
    and-expr:= assn-expr ('&&' assn-expr)*
    assn-expr:= cmp-expr (':=' assn-expr)?        (* right associative *)
    cmp-expr:= add-expr (CMP add-expr)?           (* non-associative *)
    add-expr:= mul-expr (('+'|'-') mul-expr)*
    mul-expr:= app-expr (('*'|'/') app-expr)*
    app-expr:= prefix-expr aterm*                 (* left associative *)
    prefix-expr := '-' prefix-expr | 'ref' prefix-expr | '!' prefix-expr | aterm
    aterm   := INT | 'true' | 'false' | 'unit' | IDENT | '(' expr ')'

``let rec`` only accepts a syntactic function on its right-hand side.
"""

from __future__ import annotations

from typing import Optional

from .ast_nodes import (
    App,
    Assign,
    BinOp,
    Binding,
    BoolLit,
    Deref,
    Expr,
    If,
    IntLit,
    Lam,
    Let,
    Paren,
    Program,
    Ref,
    Seq,
    UnaryOp,
    UnitLit,
    Var,
)
from .lexer import Token, lex
from .span import Pos, Span

CMP_OPS = {"=", "<>", "<", "<=", ">", ">="}
ATOM_START = {"INT", "IDENT", "true", "false", "unit", "("}


class ParseError(Exception):
    def __init__(self, message: str, span: Span):
        super().__init__(message)
        self.message = message
        self.span = span


class _Parser:
    def __init__(self, tokens: list[Token], src_len: int):
        self.toks = tokens
        self.pos = 0
        self.src_len = src_len

    # ------------------------------------------------------------- tokens
    def peek(self, off: int = 0) -> Token:
        j = self.pos + off
        if j < len(self.toks):
            return self.toks[j]
        return self.toks[-1]  # EOF token always exists

    def next(self) -> Token:
        t = self.toks[self.pos]
        if t.kind != "EOF":
            self.pos += 1
        return t

    def at(self, kind: str) -> bool:
        return self.peek().kind == kind

    def eat(self, kind: str) -> Token:
        t = self.peek()
        if t.kind != kind:
            want = "end of input" if kind == "EOF" else repr(kind)
            got = "end of input" if t.kind == "EOF" else repr(t.text)
            raise ParseError(f"expected {want} but found {got}", t.span)
        return self.next()

    def eof_span(self) -> Span:
        p = self.peek().span.start
        return Span(p, Pos(p.offset, p.line, p.col + 1))

    def span_to(self, start: Pos, end_tok: Token) -> Span:
        return Span(start, end_tok.span.end)

    # ------------------------------------------------------------- program
    def parse_program(self) -> Program:
        start = self.peek().span.start
        bindings: list[Binding] = []
        final_expr: Optional[Expr] = None

        while not self.at("EOF"):
            if self.at("let"):
                # Could be a definition (`let ... = expr ;;`) or an inline
                # `let ... in ...` expression. Parse as a definition first; if
                # the token right after the value is `in`, rewind and treat it
                # as the program's trailing expression instead.
                save = self.pos
                try:
                    tentative = self.parse_toplet()
                except ParseError:
                    self.pos = save
                    tentative = None
                if tentative is not None and not self.at("in"):
                    bindings.append(tentative)
                    if self.at_top_sep():
                        self.next()
                        self.next()
                    elif not self.at("EOF"):
                        t = self.peek()
                        raise ParseError(
                            "top-level definitions must be terminated by ';;'",
                            t.span,
                        )
                    continue
                self.pos = save
            # A single trailing expression.
            final_expr = self.parse_expr()
            if self.at_top_sep():
                self.next()
                self.next()
            if not self.at("EOF"):
                t = self.peek()
                raise ParseError(
                    "only one trailing expression is allowed; "
                    "wrap definitions in 'let ... in ...' or end them with ';;'",
                    t.span,
                )

        end = self.peek().span.start
        return Program(Span(start, end), bindings, final_expr)

    def parse_toplet(self) -> Binding:
        kw = self.eat("let")
        rec_tok = None
        if self.at("rec"):
            rec_tok = self.next()
        name_tok = self.eat("IDENT")
        self.eat("=")
        value = self.parse_expr()
        if rec_tok is not None and not isinstance(value, Lam):
            raise ParseError(
                "'let rec' may only bind a function (expected 'fun ... -> ...')",
                value.span,
            )
        return Binding(
            Span(kw.span.start, value.span.end),
            name_tok.text,
            value,
            rec_tok is not None,
            name_tok.span,
        )

    # ------------------------------------------------------------- expr
    def parse_expr(self) -> Expr:
        t = self.peek()
        if t.kind == "let":
            return self.parse_let()
        if t.kind == "fun":
            return self.parse_fun()
        if t.kind == "if":
            return self.parse_if()
        return self.parse_seq()

    def parse_nonseq(self) -> Expr:
        """A full expression that cannot be a top-level sequence.

        Used as the right-hand side of ``;`` so that ``e ; let x = .. in ..``
        works without making ``;`` right-associative (let/fun/if bodies still
        greedily absorb following ``;`` via their own parse_expr calls).
        """
        t = self.peek()
        if t.kind == "let":
            return self.parse_let()
        if t.kind == "fun":
            return self.parse_fun()
        if t.kind == "if":
            return self.parse_if()
        return self.parse_or()

    def parse_let(self) -> Expr:
        kw = self.eat("let")
        is_rec = False
        rec_tok = None
        if self.at("rec"):
            rec_tok = self.next()
            is_rec = True
        name_tok = self.eat("IDENT")
        self.eat("=")
        value = self.parse_expr()
        if is_rec and not isinstance(value, Lam):
            raise ParseError(
                "'let rec' may only bind a function (expected 'fun ... -> ...')",
                value.span,
            )
        self.eat("in")
        body = self.parse_expr()
        return Let(
            Span(kw.span.start, body.span.end),
            name_tok.text,
            value,
            body,
            is_rec,
            name_tok.span,
        )

    def parse_fun(self) -> Expr:
        kw = self.eat("fun")
        name_tok = self.eat("IDENT")
        arrow = self.eat("->")
        body = self.parse_expr()
        return Lam(
            Span(kw.span.start, body.span.end),
            name_tok.text,
            body,
            name_tok.span,
        )

    def parse_if(self) -> Expr:
        kw = self.eat("if")
        cond = self.parse_expr()
        self.eat("then")
        then_e = self.parse_expr()
        els_e = None
        if self.at("else"):
            self.next()
            els_e = self.parse_expr()
        end = (els_e or then_e).span.end
        return If(Span(kw.span.start, end), cond, then_e, els_e)

    def at_top_sep(self) -> bool:
        """True at the two-semicolon top-level terminator ``;;``."""
        return self.peek().kind == ";" and self.peek(1).kind == ";"

    def parse_seq(self) -> Expr:
        first = self.parse_or()
        if not self.at(";") or self.at_top_sep():
            return first
        start = first.span.start
        result = first
        while self.at(";") and not self.at_top_sep():
            semi = self.next()
            if self.at(";") or self.at("EOF"):
                raise ParseError(
                    "expected an expression after ';'",
                    semi.span if self.at("EOF") else self.peek().span,
                )
            right = self.parse_nonseq()
            result = Seq(Span(start, right.span.end), result, right)
        return result

    def parse_or(self) -> Expr:
        left = self.parse_and()
        if not self.at("||"):
            return left
        start = left.span.start
        while self.at("||"):
            op = self.next()
            right = self.parse_and()
            left = BinOp(Span(start, right.span.end), "||", left, right, op.span)
        return left

    def parse_and(self) -> Expr:
        left = self.parse_assign()
        if not self.at("&&"):
            return left
        start = left.span.start
        while self.at("&&"):
            op = self.next()
            right = self.parse_assign()
            left = BinOp(Span(start, right.span.end), "&&", left, right, op.span)
        return left

    def parse_assign(self) -> Expr:
        target = self.parse_cmp()
        if self.at(":="):
            op = self.next()
            value = self.parse_assign()  # right associative
            return Assign(Span(target.span.start, value.span.end), target, value)
        return target

    def parse_cmp(self) -> Expr:
        left = self.parse_add()
        if self.peek().kind not in CMP_OPS:
            return left
        op = self.next()
        right = self.parse_add()
        if self.peek().kind in CMP_OPS:
            t = self.peek()
            raise ParseError(
                "comparison operators cannot be chained (use '&&' / '||')", t.span
            )
        return BinOp(Span(left.span.start, right.span.end), op.kind, left, right, op.span)

    def parse_add(self) -> Expr:
        left = self.parse_mul()
        start = left.span.start
        while self.peek().kind in ("+", "-"):
            op = self.next()
            right = self.parse_mul()
            left = BinOp(Span(start, right.span.end), op.kind, left, right, op.span)
        return left

    def parse_mul(self) -> Expr:
        left = self.parse_app()
        start = left.span.start
        while self.peek().kind in ("*", "/"):
            op = self.next()
            right = self.parse_app()
            left = BinOp(Span(start, right.span.end), op.kind, left, right, op.span)
        return left

    def parse_app(self) -> Expr:
        fn = self.parse_prefix()
        start = fn.span.start
        while self.peek().kind in ATOM_START:
            arg = self.parse_aterm()
            fn = App(Span(start, arg.span.end), fn, arg)
        return fn

    def parse_prefix(self) -> Expr:
        t = self.peek()
        if t.kind == "-":
            self.next()
            inner = self.parse_prefix()
            return UnaryOp(Span(t.span.start, inner.span.end), "-", inner, t.span)
        if t.kind == "ref":
            self.next()
            inner = self.parse_prefix()
            return Ref(Span(t.span.start, inner.span.end), inner)
        if t.kind == "!":
            self.next()
            inner = self.parse_prefix()
            return Deref(Span(t.span.start, inner.span.end), inner)
        return self.parse_aterm()

    def parse_aterm(self) -> Expr:
        t = self.peek()
        if t.kind == "INT":
            self.next()
            return IntLit(t.span, int(t.text))
        if t.kind in ("true", "false"):
            self.next()
            return BoolLit(t.span, t.kind == "true")
        if t.kind == "unit":
            self.next()
            return UnitLit(t.span)
        if t.kind == "IDENT":
            self.next()
            return Var(t.span, t.text)
        if t.kind == "(":
            lparen = self.next()
            inner = self.parse_expr()
            rparen = self.eat(")")
            return Paren(Span(lparen.span.start, rparen.span.end), inner)
        if t.kind == "":
            raise ParseError("expected an expression but found end of input", t.span)
        raise ParseError(f"expected an expression but found {t.text!r}", t.span)


def parse(src: str) -> Program:
    """Parse MiniML source text into a :class:`Program`."""
    tokens = lex(src)
    p = _Parser(tokens, len(src))
    return p.parse_program()
