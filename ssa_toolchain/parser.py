"""Recursive-descent parser for the mini language.

Grammar (documented in LANGUAGE.md)::

    program   := func*
    func      := 'func' IDENT '(' [IDENT (',' IDENT)*] ')' block
    block     := '{' stmt* '}'
    stmt      := 'var'   IDENT '=' expr ';'
               | IDENT   '=' expr ';'
               | 'if' '(' expr ')' block ('else' (block | if-stmt))?
               | 'while' '(' expr ')' block
               | 'return' [expr] ';'
               | block
               | expr ';'

    expr      := binary with precedence; unary -, !, ~; calls; literals.
"""
from __future__ import annotations

from . import ast_nodes as ast
from .errors import ParseError
from .lexer import Token, tokenize_mini

# Operator precedence table (higher binds tighter).
_BIN_PREC = {
    "||": 1,
    "&&": 2,
    "|": 3,
    "^": 4,
    "&": 5,
    "==": 6, "!=": 6,
    "<": 7, "<=": 7, ">": 7, ">=": 7,
    "<<": 8, ">>": 8,
    "+": 9, "-": 9,
    "*": 10, "/": 10, "%": 10,
}


class Parser:
    def __init__(self, tokens: list[Token], file: str = "<input>"):
        self.toks = tokens
        self.pos = 0
        self.file = file

    # -- token helpers ------------------------------------------------------

    @property
    def cur(self) -> Token:
        return self.toks[self.pos]

    def _eat(self) -> Token:
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def accept(self, kind: str) -> Token | None:
        if self.cur.kind == kind:
            return self._eat()
        return None

    def expect(self, kind: str, what: str | None = None) -> Token:
        if self.cur.kind != kind:
            want = what or kind
            raise ParseError(f"expected {want} but found {self._describe(self.cur)}",
                             self.cur.loc)
        return self._eat()

    @staticmethod
    def _describe(t: Token) -> str:
        if t.kind == "eof":
            return "end of file"
        return f"{t.kind} {t.value!r}" if t.kind in ("identifier", "integer") else f"{t.value!r}"

    # -- grammar ------------------------------------------------------------

    def parse_program(self) -> ast.Program:
        start = self.cur.loc
        funcs = []
        while self.cur.kind != "eof":
            funcs.append(self.parse_func())
        loc = start.merge(self.cur.loc)
        return ast.Program(loc=loc, funcs=tuple(funcs))

    def parse_func(self) -> ast.Func:
        start = self.expect("func", "keyword 'func'").loc
        name_tok = self.expect("identifier", "function name")
        self.expect("(")
        params: list[str] = []
        if self.cur.kind != ")":
            while True:
                p = self.expect("identifier", "parameter name")
                if p.value in params:
                    raise ParseError(f"duplicate parameter {p.value!r}", p.loc)
                params.append(p.value)
                if not self.accept(","):
                    break
        self.expect(")")
        body_loc = self.cur.loc
        self.expect("{")
        declared = {p: True for p in params}
        body = self.parse_stmts_until_decl("}", declared)
        end = self.expect("}")
        return ast.Func(
            loc=start.merge(name_tok.loc),
            name=name_tok.value,
            params=tuple(params),
            body=tuple(body),
            loc_end=body_loc.merge(end.loc),
        )

    def parse_block(self, declared) -> ast.Block:
        open_tok = self.expect("{")
        stmts = self.parse_stmts_until_decl("}", declared)
        close_tok = self.expect("}")
        return ast.Block(loc=open_tok.loc.merge(close_tok.loc),
                         stmts=tuple(stmts))

    def parse_stmts_until_decl(self, close: str, declared) -> list:
        stmts = []
        while self.cur.kind != close and self.cur.kind != "eof":
            stmts.append(self.parse_stmt(declared))
        if self.cur.kind == "eof":
            raise ParseError(f"expected {close!r} but reached end of file",
                             self.cur.loc)
        return stmts

    def parse_stmt(self, declared=None):
        if declared is None:
            declared = {}
        k = self.cur.kind
        if k == "{":
            return self.parse_block(declared)
        if k == "var":
            return self.parse_var(declared)
        if k == "if":
            return self.parse_if(declared)
        if k == "while":
            return self.parse_while(declared)
        if k == "return":
            return self.parse_return()
        if k == "identifier" and self.toks[self.pos + 1].kind == "=":
            return self.parse_assign(declared)
        return self.parse_expr_stmt()

    def parse_var(self, declared) -> ast.VarDecl:
        kw = self._eat()
        name_tok = self.expect("identifier", "variable name")
        if name_tok.value in declared:
            raise ParseError(
                f"variable {name_tok.value!r} already declared",
                name_tok.loc)
        self.expect("=")
        init = self.parse_expr()
        semi = self.expect(";")
        if name_tok.value.startswith("__"):
            raise ParseError("identifiers starting with '__' are reserved",
                             name_tok.loc)
        declared[name_tok.value] = True
        return ast.VarDecl(loc=kw.loc.merge(semi.loc),
                           name=name_tok.value, init=init)

    def parse_assign(self, declared) -> ast.Assign:
        name_tok = self._eat()
        self._eat()  # '='
        value = self.parse_expr()
        semi = self.expect(";")
        return ast.Assign(loc=name_tok.loc.merge(semi.loc),
                          name=name_tok.value, value=value)

    def parse_if(self, declared) -> ast.If:
        kw = self._eat()
        self.expect("(")
        cond = self.parse_expr()
        self.expect(")")
        then_block = self.parse_block(declared)
        else_body: tuple = ()
        if self.accept("else"):
            if self.cur.kind == "if":
                else_body = (self.parse_if(declared),)
            else:
                else_body = (self.parse_block(declared),)
        return ast.If(
            loc=kw.loc.merge((else_body[-1].loc if else_body
                              else then_block.loc)),
            cond=cond,
            then_body=tuple(then_block.stmts),
            else_body=else_body,
        )

    def parse_while(self, declared) -> ast.While:
        kw = self._eat()
        self.expect("(")
        cond = self.parse_expr()
        self.expect(")")
        body = self.parse_block(declared)
        return ast.While(loc=kw.loc.merge(body.loc), cond=cond,
                         body=tuple(body.stmts))

    def parse_return(self) -> ast.Return:
        kw = self._eat()
        value = None
        if self.cur.kind != ";":
            value = self.parse_expr()
        semi = self.expect(";")
        return ast.Return(loc=kw.loc.merge(semi.loc), value=value)

    def parse_expr_stmt(self) -> ast.ExprStmt:
        e = self.parse_expr()
        semi = self.expect(";")
        return ast.ExprStmt(loc=e.loc.merge(semi.loc), expr=e)

    # -- expressions (Pratt) -------------------------------------------------

    def parse_expr(self, min_prec: int = 1) -> object:
        left = self.parse_unary()
        while True:
            op = self.cur.kind
            prec = _BIN_PREC.get(op)
            if prec is None or prec < min_prec:
                return left
            self._eat()  # operator
            right = self.parse_expr(prec + 1)  # left associative
            left = ast.Binary(loc=left.loc.merge(right.loc), op=op,
                              left=left, right=right)

    def parse_unary(self) -> object:
        if self.cur.kind in ("-", "!", "~"):
            op_tok = self._eat()
            arg = self.parse_unary()
            return ast.Unary(loc=op_tok.loc.merge(arg.loc), op=op_tok.kind, arg=arg)
        return self.parse_primary()

    def parse_primary(self) -> object:
        t = self.cur
        if t.kind == "integer":
            self._eat()
            return ast.IntLit(loc=t.loc, value=int(t.value))
        if t.kind in ("true", "false"):
            self._eat()
            return ast.BoolLit(loc=t.loc, value=(t.kind == "true"))
        if t.kind == "(":
            self._eat()
            e = self.parse_expr()
            self.expect(")")
            return e
        if t.kind == "identifier":
            self._eat()
            if self.cur.kind == "(":
                self._eat()
                args = []
                if self.cur.kind != ")":
                    while True:
                        args.append(self.parse_expr())
                        if not self.accept(","):
                            break
                close = self.expect(")")
                return ast.Call(loc=t.loc.merge(close.loc), name=t.value,
                                args=tuple(args))
            return ast.Name(loc=t.loc, name=t.value)
        raise ParseError(f"expected an expression but found {self._describe(t)}", t.loc)


def parse(source: str, file: str = "<input>") -> ast.Program:
    tokens = tokenize_mini(source, file)
    return Parser(tokens, file).parse_program()
