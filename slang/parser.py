"""Hand-written recursive-descent parser for Slang.

Consumes the token stream produced by :mod:`slang.lexer` and builds the AST
from :mod:`slang.ast_nodes`.  No parser library or grammar generator is used.
Precedence climbing handles binary operators; function calls are postfix.
"""

from __future__ import annotations

from . import ast_nodes as ast
from .errors import Loc, ParseError
from .lexer import Token, tokenize

# Binary operators in precedence-then-associativity table.
BINARY_PRECEDENCE = {
    "||": 1,
    "&&": 2,
    "==": 3,
    "!=": 3,
    "<": 4,
    "<=": 4,
    ">": 4,
    ">=": 4,
    "+": 5,
    "-": 5,
    "*": 6,
    "/": 6,
    "%": 6,
}


def span(start: Loc, end: Loc) -> Loc:
    return Loc(start.line, start.col, end.end_line, end.end_col, start.offset, end.end_offset)


class Parser:
    def __init__(self, tokens: list[Token]):
        self.toks = tokens
        self.pos = 0

    # -- token helpers ------------------------------------------------------

    @property
    def cur(self) -> Token:
        return self.toks[self.pos]

    def peek_kind(self, off: int = 0) -> str:
        j = self.pos + off
        return self.toks[j].kind if j < len(self.toks) else "EOF"

    def advance(self) -> Token:
        t = self.cur
        if t.kind != "EOF":
            self.pos += 1
        return t

    def check(self, kind: str) -> bool:
        return self.cur.kind == kind

    def accept(self, kind: str) -> Token | None:
        if self.check(kind):
            return self.advance()
        return None

    def expect(self, kind: str, what: str | None = None) -> Token:
        if not self.check(kind):
            found = self.cur.text or self.cur.kind
            raise ParseError(
                f"expected {what or kind!r} but found {found!r}", self.cur.loc
            )
        return self.advance()

    # -- entry point --------------------------------------------------------

    def parse_program(self) -> ast.Program:
        start = self.cur.loc
        stmts: list = []
        while not self.check("EOF"):
            s = self.parse_stmt()
            if s is not None:
                stmts.append(s)
        return ast.Program(stmts, loc=span(start, self.cur.loc))

    # -- statements ---------------------------------------------------------

    def parse_stmt(self):
        k = self.cur.kind
        if k == ";":
            self.advance()
            return None
        if k == "{":
            return self.parse_block()
        if k == "KEYWORD:let":
            return self.parse_let()
        if k == "KEYWORD:fn":
            # Statement-level fn is always a declaration; a name is required.
            return self.parse_fn_decl()
        if k == "KEYWORD:return":
            return self.parse_return()
        if k == "KEYWORD:if":
            return self.parse_if()
        if k == "KEYWORD:while":
            return self.parse_while()
        # Assignment statement: ID '=' expr ';'  (but not '==' etc.)
        if k == "ID" and self.peek_kind(1) == "=":
            return self.parse_assign()
        return self.parse_expr_stmt()

    def parse_block(self) -> ast.Block:
        start = self.expect("{", "'{'").loc
        stmts: list = []
        while not self.check("}") and not self.check("EOF"):
            s = self.parse_stmt()
            if s is not None:
                stmts.append(s)
        end = self.expect("}", "'}'").loc
        return ast.Block(stmts, loc=span(start, end))

    def parse_let(self) -> ast.Let:
        start = self.expect("KEYWORD:let").loc
        name_tok = self.expect("ID", "identifier")
        init = None
        if self.accept("="):
            init = self.parse_expr()
        end = self.expect(";", "';'").loc
        return ast.Let(name_tok.value, init, name_loc=name_tok.loc, loc=span(start, end))

    def parse_fn_decl(self) -> ast.FnDecl:
        start = self.expect("KEYWORD:fn").loc
        name_tok = self.expect("ID", "function name")
        params = self.parse_params()
        body = self.parse_block()
        return ast.FnDecl(name_tok.value, params, body, name_loc=name_tok.loc, loc=span(start, body.loc))

    def parse_params(self) -> list:
        self.expect("(", "'('")
        params: list = []
        if not self.check(")"):
            while True:
                p = self.expect("ID", "parameter name")
                params.append(ast.Param(p.value, name_loc=p.loc, loc=p.loc))
                if not self.accept(","):
                    break
        self.expect(")", "')'")
        return params

    def parse_return(self) -> ast.Return:
        start = self.expect("KEYWORD:return").loc
        value = None
        # A return value is anything that can start an expression.
        if not self.check(";") and not self.check("}"):
            value = self.parse_expr()
        end = self.expect(";", "';'").loc
        return ast.Return(value, loc=span(start, end))

    def parse_if(self) -> ast.If:
        start = self.expect("KEYWORD:if").loc
        self.expect("(", "'('")
        cond = self.parse_expr()
        self.expect(")", "')'")
        then = self.parse_block()
        otherwise = None
        if self.accept("KEYWORD:else"):
            if self.check("KEYWORD:if"):
                otherwise = self.parse_if()
            else:
                otherwise = self.parse_block()
        return ast.If(cond, then, otherwise, loc=span(start, otherwise.loc if otherwise else then.loc))

    def parse_while(self) -> ast.While:
        start = self.expect("KEYWORD:while").loc
        self.expect("(", "'('")
        cond = self.parse_expr()
        self.expect(")", "')'")
        body = self.parse_block()
        return ast.While(cond, body, loc=span(start, body.loc))

    def parse_assign(self) -> ast.Assign:
        name_tok = self.advance()  # ID
        self.expect("=", "'='")
        value = self.parse_expr()
        end = self.expect(";", "';'").loc
        return ast.Assign(name_tok.value, value, name_loc=name_tok.loc, loc=span(name_tok.loc, end))

    def parse_expr_stmt(self) -> ast.ExprStmt:
        start = self.cur.loc
        e = self.parse_expr()
        end = self.expect(";", "';'").loc
        return ast.ExprStmt(e, loc=span(start, end))

    # -- expressions --------------------------------------------------------

    def parse_expr(self) -> ast.Node:
        return self.parse_binary(min_prec=1)

    def parse_binary(self, min_prec: int) -> ast.Node:
        left = self.parse_unary()
        while True:
            op = self.cur.kind
            prec = BINARY_PRECEDENCE.get(op)
            if prec is None or prec < min_prec:
                break
            op_tok = self.advance()
            right = self.parse_binary(prec + 1)  # left-associative
            left = ast.Binary(op_tok.text, left, right, loc=span(left.loc, right.loc))
        return left

    def parse_unary(self) -> ast.Node:
        if self.cur.kind in ("-", "!"):
            op_tok = self.advance()
            operand = self.parse_unary()
            return ast.Unary(op_tok.text, operand, loc=span(op_tok.loc, operand.loc))
        return self.parse_call()

    def parse_call(self) -> ast.Node:
        e = self.parse_primary()
        while self.accept("("):
            start = e.loc
            args: list = []
            if not self.check(")"):
                while True:
                    args.append(self.parse_expr())
                    if not self.accept(","):
                        break
            end = self.expect(")", "')'").loc
            e = ast.Call(e, args, loc=span(start, end))
        return e

    def parse_primary(self) -> ast.Node:
        t = self.cur
        k = t.kind
        if k == "INT":
            self.advance()
            return ast.IntLit(t.value, loc=t.loc)
        if k == "STR":
            self.advance()
            return ast.StrLit(t.value, loc=t.loc)
        if k == "KEYWORD:true":
            self.advance()
            return ast.BoolLit(True, loc=t.loc)
        if k == "KEYWORD:false":
            self.advance()
            return ast.BoolLit(False, loc=t.loc)
        if k == "KEYWORD:null":
            self.advance()
            return ast.NullLit(loc=t.loc)
        if k == "ID":
            self.advance()
            return ast.Var(t.value, name_loc=t.loc, loc=t.loc)
        if k == "(":
            self.advance()
            e = self.parse_expr()
            self.expect(")", "')'")
            return e
        if k == "KEYWORD:fn":
            return self.parse_fn_expr()
        raise ParseError(f"expected an expression but found {t.text or t.kind!r}", t.loc)

    def parse_fn_expr(self) -> ast.FnExpr:
        start = self.expect("KEYWORD:fn").loc
        name = None
        name_loc = None
        if self.check("ID"):
            nt = self.advance()
            name = nt.value
            name_loc = nt.loc
        params = self.parse_params()
        body = self.parse_block()
        return ast.FnExpr(name, params, body, name_loc=name_loc, loc=span(start, body.loc))


def parse(source: str, filename: str = "<input>") -> ast.Program:
    toks = tokenize(source, filename)
    return Parser(toks).parse_program()
