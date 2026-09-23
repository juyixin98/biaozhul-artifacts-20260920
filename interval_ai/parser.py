"""Hand-written recursive-descent parser for IntervalLang.

This is a conventional parser written from scratch (no parser generators, no
compiler libraries).  See README.md for the full EBNF grammar.
"""

from __future__ import annotations

from . import ast_nodes as ast
from .errors import ParseError, Span
from .lexer import Token, Lexer


class Parser:
    def __init__(self, tokens: list[Token], source: str):
        self.toks = tokens
        self.src = source
        self.p = 0

    # ---- token helpers ---------------------------------------------------

    @property
    def tok(self) -> Token:
        return self.toks[self.p]

    def peek_kind(self, k: int = 0) -> str:
        return self.toks[min(self.p + k, len(self.toks) - 1)].kind

    def advance(self) -> Token:
        t = self.tok
        if t.kind != "EOF":
            self.p += 1
        return t

    def at(self, kind: str) -> bool:
        return self.tok.kind == kind

    def eat(self, kind: str, what: str | None = None) -> Token:
        if self.tok.kind != kind:
            raise self.error(f"expected {what or kind!r}, got {self.tok.text!r}",
                             self.tok.span)
        return self.advance()

    def error(self, msg: str, span: Span) -> ParseError:
        return ParseError(msg, span)

    # ---- entry point -----------------------------------------------------

    def parse_program(self) -> ast.Program:
        start = self.tok.span
        decls: list[ast.Decl] = []
        seen: set[str] = set()

        # Declarations:  var x [= e]; | input var x; | input array a[INT];
        while self.at("var") or (self.at("input")
                                 and self.peek_kind(1) in ("var", "array")):
            new_decls = self.parse_decl()
            for d in new_decls:
                if d.name in seen:
                    raise self.error(f"duplicate declaration of {d.name!r}",
                                     d.span)
                seen.add(d.name)
            decls.extend(new_decls)

        # Body: either an explicit { ... } block or a bare sequence of
        # statements up to end of file (implicit program body).
        if self.at("{"):
            body = self.parse_block()
        else:
            start = self.tok.span
            stmts: list[ast.Stmt] = []
            while not self.at("EOF"):
                stmts.append(self.parse_stmt())
            end = self.tok.span
            body = ast.Block(stmts, Span(start.start_offset, end.start_offset,
                                         start.line, start.col))
        if not self.at("EOF"):
            raise self.error(f"unexpected token {self.tok.text!r}", self.tok.span)
        return ast.Program(decls, body,
                           Span(start.start_offset, body.span.end_offset,
                                start.line, start.col))

    def parse_decl(self) -> list[ast.Decl]:
        if self.at("input"):
            inp = self.advance()
            if self.at("var"):
                self.advance()
                name_t = self.eat("IDENT", "identifier")
                self.eat(";")
                return [ast.Decl(name_t.text, name_t.span, "input")]
            self.eat("array")
            name_t = self.eat("IDENT", "identifier")
            self.eat("[")
            len_t = self.eat("INT", "non-negative array length")
            if len_t.value < 0:
                raise self.error("array length must be non-negative", len_t.span)
            self.eat("]")
            self.eat(";")
            return [ast.Decl(name_t.text, name_t.span, "array",
                             length=len_t.value, length_span=len_t.span)]

        # var name [= expr] ;
        self.eat("var")
        name_t = self.eat("IDENT", "identifier")
        init = None
        if self.eat_if("="):
            init = self.parse_expr()
        self.eat(";")
        return [ast.Decl(name_t.text, name_t.span, "var", init=init)]

    def eat_if(self, kind: str) -> bool:
        if self.at(kind):
            self.advance()
            return True
        return False

    # ---- statements ------------------------------------------------------

    def parse_block(self) -> ast.Block:
        start = self.tok.span
        self.eat("{", "'{'")
        stmts: list[ast.Stmt] = []
        while not self.at("}"):
            if self.at("EOF"):
                raise self.error("unexpected end of file, expected '}'",
                                 self.tok.span)
            stmts.append(self.parse_stmt())
        end = self.advance()  # }
        return ast.Block(stmts, Span(start.start_offset, end.span.end_offset,
                                     start.line, start.col))

    def parse_stmt(self) -> ast.Stmt:
        t = self.tok

        if self.at("{"):
            return self.parse_block()

        if self.at(";"):
            semi = self.advance()
            return ast.Skip(semi.span)

        if self.at("var"):
            # Local scalar declaration:  var name [= expr] ;
            self.advance()
            name_t = self.eat("IDENT", "identifier")
            init = None
            if self.eat_if("="):
                init = self.parse_expr()
            self.eat(";")
            return ast.LocalDecl(name_t.text, init, name_t.span)

        if self.at("skip"):
            kw = self.advance()
            self.eat(";")
            return ast.Skip(Span(kw.span.start_offset, kw.span.end_offset,
                                 kw.span.line, kw.span.col))

        if self.at("if"):
            return self.parse_if()

        if self.at("while"):
            return self.parse_while()

        if self.at("input"):
            self.advance()
            name_t = self.eat("IDENT", "identifier")
            self.eat(";")
            return ast.InputStmt(name_t.text,
                                 Span(t.span.start_offset,
                                      self.toks[self.p - 1].span.end_offset,
                                      t.span.line, t.span.col),
                                 name_t.span)

        if self.at("havoc"):
            self.advance()
            name_t = self.eat("IDENT", "identifier")
            self.eat(";")
            return ast.HavocStmt(name_t.text,
                                 Span(t.span.start_offset,
                                      self.toks[self.p - 1].span.end_offset,
                                      t.span.line, t.span.col),
                                 name_t.span)

        if self.at("IDENT"):
            name_t = self.advance()
            if self.eat_if("["):
                idx = self.parse_expr()
                self.eat("]")
                self.eat("=", "'='")
                val = self.parse_expr()
                semi = self.eat(";")
                return ast.ArrayStore(name_t.text, idx, val,
                                      Span(t.span.start_offset,
                                           semi.span.end_offset,
                                           t.span.line, t.span.col),
                                      name_t.span)
            self.eat("=", "'='")
            val = self.parse_expr()
            semi = self.eat(";")
            return ast.Assign(name_t.text, val,
                              Span(t.span.start_offset, semi.span.end_offset,
                                   t.span.line, t.span.col),
                              name_t.span)

        raise self.error(f"unexpected token {t.text!r}", t.span)

    def parse_if(self) -> ast.If:
        kw = self.advance()
        self.eat("(")
        cond = self.parse_expr()
        self.eat(")")
        then = self.parse_block()
        else_ = None
        if self.eat_if("else"):
            else_ = self.parse_block()
        end = else_.span.end_offset if else_ else then.span.end_offset
        return ast.If(cond, then, else_,
                      Span(kw.span.start_offset, end, kw.span.line, kw.span.col))

    def parse_while(self) -> ast.While:
        kw = self.advance()
        self.eat("(")
        cond = self.parse_expr()
        self.eat(")")
        body = self.parse_block()
        return ast.While(cond, body,
                         Span(kw.span.start_offset, body.span.end_offset,
                              kw.span.line, kw.span.col))

    # ---- expressions (precedence climbing) -------------------------------

    # (left-prec, right-prec).  Comparison operators use a right precedence
    # one above their left precedence, which makes them non-associative:
    # "a < b < c" fails to parse rather than silently grouping either way.
    _BINARY_PRECEDENCE = {
        "||": (1, 2),
        "&&": (3, 4),
        "==": (5, 6), "!=": (5, 6),
        "<": (6, 7), "<=": (6, 7), ">": (6, 7), ">=": (6, 7),
        "+": (7, 8), "-": (7, 8),
        "*": (9, 10), "/": (9, 10), "%": (9, 10),
    }

    def parse_expr(self) -> ast.Expr:
        return self.parse_binary(0)

    _COMPARISONS = {"==", "!=", "<", "<=", ">", ">="}

    def parse_binary(self, min_prec: int) -> ast.Expr:
        left = self.parse_unary()
        while self.tok.kind in self._BINARY_PRECEDENCE:
            op = self.tok.kind
            prec_l, prec_r = self._BINARY_PRECEDENCE[op]
            if prec_l < min_prec:
                break
            op_tok = self.advance()
            right = self.parse_binary(prec_r)
            span = Span(left.span.start_offset, right.span.end_offset,
                        left.span.line, left.span.col)
            left = ast.Binary(op, left, right, span, op_tok.span)
            # Comparisons are non-associative: reject a directly adjacent
            # second comparison (a < b < c); parenthesize explicitly instead.
            if op in self._COMPARISONS and self.tok.kind in self._COMPARISONS:
                raise self.error(
                    "comparisons are non-associative (use parentheses or "
                    "&& to combine them)", self.tok.span)
        return left

    def parse_unary(self) -> ast.Expr:
        if self.at("-") or self.at("!"):
            op_tok = self.advance()
            operand = self.parse_unary()
            return ast.Unary(op_tok.kind, operand,
                             Span(op_tok.span.start_offset,
                                  operand.span.end_offset,
                                  op_tok.span.line, op_tok.span.col))
        return self.parse_postfix()

    def parse_postfix(self) -> ast.Expr:
        atom = self.parse_atom()
        while self.at("["):
            br = self.advance()
            if not isinstance(atom, ast.Var):
                raise self.error("only array variables may be indexed",
                                 br.span)
            idx = self.parse_expr()
            self.eat("]")
            atom = ast.ArrayLoad(atom.name, idx,
                                 Span(atom.span.start_offset,
                                      self.toks[self.p - 1].span.end_offset,
                                      atom.span.line, atom.span.col),
                                 atom.span)
        return atom

    def parse_atom(self) -> ast.Expr:
        t = self.tok
        if self.at("INT"):
            self.advance()
            return ast.IntLit(t.value, t.span)
        if self.at("true") or self.at("false"):
            self.advance()
            return ast.BoolLit(t.kind == "true", t.span)
        if self.at("IDENT"):
            self.advance()
            return ast.Var(t.text, t.span)
        if self.at("("):
            self.advance()
            e = self.parse_expr()
            self.eat(")")
            return e
        raise self.error(f"unexpected token {t.text!r} in expression", t.span)


def parse_source(source: str) -> ast.Program:
    tokens = Lexer(source).tokenize()
    return Parser(tokens, source).parse_program()
