"""Recursive-descent parser for TaintLang.

Grammar (informal, see README for prose):

    program   := (funcdef | statement)*
    funcdef   := 'func' NAME '(' params? ')' block
    block     := '{' statement* '}'
    statement := block
               |  'if' '(' expr ')' statement ('else' statement)?
               |  'while' '(' expr ')' statement
               |  'return' expr? ';'
               |  NAME '=' expr ';'
               |  expr ';'
    expr      := or_expr
    or_expr   := and_expr ('||' and_expr)*
    and_expr  := cmp_expr ('&&' cmp_expr)*
    cmp_expr  := add_expr (('=='|'!='|'<'|'<='|'>'|'>=') add_expr)*
    add_expr  := mul_expr (('+'|'-') mul_expr)*
    mul_expr  := unary (('*'|'/'|'%') unary)*
    unary     := ('!'|'-') unary | primary
    primary   := INT | STRING | 'true' | 'false' | 'nil'
               |  NAME '(' args? ')' | NAME | '(' expr ')'
"""
from __future__ import annotations

from . import ast_nodes as ast
from .lexer import Lexer, Loc, Token, TokenType


class ParseError(Exception):
    def __init__(self, message: str, loc: Loc):
        super().__init__(f"{message} at line {loc.line}, col {loc.col}")
        self.loc = loc


class Parser:
    def __init__(self, tokens: list[Token]):
        self.toks = tokens
        self.pos = 0

    # ---- token helpers ----

    def _cur(self) -> Token:
        return self.toks[self.pos]

    def _advance(self) -> Token:
        tok = self.toks[self.pos]
        if tok.type is not TokenType.EOF:
            self.pos += 1
        return tok

    def _check(self, ttype: TokenType) -> bool:
        return self._cur().type is ttype

    def _match(self, *types: TokenType) -> Token | None:
        if self._cur().type in types:
            return self._advance()
        return None

    def _expect(self, ttype: TokenType, what: str) -> Token:
        tok = self._cur()
        if tok.type is not ttype:
            label = repr(tok.value) if tok.value else tok.type.name
            raise ParseError(f"expected {what} but found {label}", tok.loc)
        return self._advance()

    # ---- entry ----

    def parse_program(self) -> ast.Program:
        prog = ast.Program(loc=Loc(1, 1), funcs={}, func_order=[])
        while not self._check(TokenType.EOF):
            if self._check(TokenType.FUNC):
                fn = self._parse_func()
                if fn.name in prog.funcs:
                    raise ParseError(f"function {fn.name!r} redefined", fn.loc)
                if fn.name in ast.BUILTINS:
                    raise ParseError(f"cannot redefine builtin {fn.name!r}", fn.loc)
                prog.funcs[fn.name] = fn
                prog.func_order.append(fn.name)
            else:
                prog.top_level.append(self._parse_stmt())
        return prog

    def _parse_func(self) -> ast.FuncDef:
        loc = self._expect(TokenType.FUNC, "'func'").loc
        name_tok = self._expect(TokenType.NAME, "function name")
        self._expect(TokenType.LPAREN, "'('")
        params: list[str] = []
        if not self._check(TokenType.RPAREN):
            while True:
                p = self._expect(TokenType.NAME, "parameter name")
                if p.value in params:
                    raise ParseError(f"duplicate parameter {p.value!r}", p.loc)
                params.append(p.value)
                if not self._match(TokenType.COMMA):
                    break
        self._expect(TokenType.RPAREN, "')'")
        body = self._parse_block()
        return ast.FuncDef(loc=loc, name=name_tok.value, params=params, body=body)

    # ---- statements ----

    def _parse_block(self) -> ast.Block:
        loc = self._expect(TokenType.LBRACE, "'{'").loc
        stmts: list[ast.Stmt] = []
        while not self._check(TokenType.RBRACE) and not self._check(TokenType.EOF):
            stmts.append(self._parse_stmt())
        self._expect(TokenType.RBRACE, "'}'")
        return ast.Block(loc=loc, body=stmts)

    def _parse_stmt(self) -> ast.Stmt:
        tok = self._cur()
        if tok.type is TokenType.LBRACE:
            return self._parse_block()
        if tok.type is TokenType.IF:
            return self._parse_if()
        if tok.type is TokenType.WHILE:
            return self._parse_while()
        if tok.type is TokenType.RETURN:
            return self._parse_return()
        if tok.type is TokenType.NAME and self.toks[self.pos + 1].type is TokenType.ASSIGN:
            self._advance()  # NAME
            self._advance()  # '='
            value = self._parse_expr()
            self._expect(TokenType.SEMI, "';'")
            return ast.Assign(loc=tok.loc, name=tok.value, value=value)
        # expression statement
        expr = self._parse_expr()
        self._expect(TokenType.SEMI, "';'")
        return ast.ExprStmt(loc=tok.loc, expr=expr)

    def _parse_if(self) -> ast.If:
        loc = self._expect(TokenType.IF, "'if'").loc
        self._expect(TokenType.LPAREN, "'('")
        cond = self._parse_expr()
        self._expect(TokenType.RPAREN, "')'")
        then = self._parse_stmt()
        otherwise: ast.Stmt | None = None
        if self._match(TokenType.ELSE):
            otherwise = self._parse_stmt()
        return ast.If(loc=loc, cond=cond, then=then, otherwise=otherwise)

    def _parse_while(self) -> ast.While:
        loc = self._expect(TokenType.WHILE, "'while'").loc
        self._expect(TokenType.LPAREN, "'('")
        cond = self._parse_expr()
        self._expect(TokenType.RPAREN, "')'")
        body = self._parse_stmt()
        return ast.While(loc=loc, cond=cond, body=body)

    def _parse_return(self) -> ast.Return:
        loc = self._expect(TokenType.RETURN, "'return'").loc
        value: ast.Expr | None = None
        if not self._check(TokenType.SEMI):
            value = self._parse_expr()
        else:
            value = ast.NilLit(loc=loc)
        self._expect(TokenType.SEMI, "';'")
        return ast.Return(loc=loc, value=value)

    # ---- expressions ----

    _CMP = {
        TokenType.EQ: "==",
        TokenType.NEQ: "!=",
        TokenType.LT: "<",
        TokenType.LE: "<=",
        TokenType.GT: ">",
        TokenType.GE: ">=",
    }
    _ADD = {TokenType.PLUS: "+", TokenType.MINUS: "-"}
    _MUL = {TokenType.STAR: "*", TokenType.SLASH: "/", TokenType.PERCENT: "%"}

    def _parse_expr(self) -> ast.Expr:
        return self._parse_or()

    def _bin_loop(self, sub, ops: dict[TokenType, str]) -> ast.Expr:
        left = sub()
        while self._cur().type in ops:
            op_tok = self._advance()
            right = sub()
            left = ast.Binary(loc=op_tok.loc, op=ops[op_tok.type], left=left, right=right)
        return left

    def _parse_or(self) -> ast.Expr:
        left = self._parse_and()
        while self._match(TokenType.OR):
            loc = self.toks[self.pos - 1].loc
            right = self._parse_and()
            left = ast.Binary(loc=loc, op="||", left=left, right=right)
        return left

    def _parse_and(self) -> ast.Expr:
        left = self._parse_cmp()
        while self._match(TokenType.AND):
            loc = self.toks[self.pos - 1].loc
            right = self._parse_cmp()
            left = ast.Binary(loc=loc, op="&&", left=left, right=right)
        return left

    def _parse_cmp(self) -> ast.Expr:
        return self._bin_loop(self._parse_add, self._CMP)

    def _parse_add(self) -> ast.Expr:
        return self._bin_loop(self._parse_mul, self._ADD)

    def _parse_mul(self) -> ast.Expr:
        return self._bin_loop(self._parse_unary, self._MUL)

    def _parse_unary(self) -> ast.Expr:
        tok = self._cur()
        if tok.type in (TokenType.NOT, TokenType.MINUS):
            self._advance()
            return ast.Unary(loc=tok.loc, op="!" if tok.type is TokenType.NOT else "-",
                             operand=self._parse_unary())
        return self._parse_primary()

    def _parse_primary(self) -> ast.Expr:
        tok = self._cur()
        if tok.type is TokenType.INT:
            self._advance()
            return ast.IntLit(loc=tok.loc, value=int(tok.value))
        if tok.type is TokenType.STRING:
            self._advance()
            return ast.StrLit(loc=tok.loc, value=tok.value)
        if tok.type in (TokenType.TRUE, TokenType.FALSE):
            self._advance()
            return ast.BoolLit(loc=tok.loc, value=tok.type is TokenType.TRUE)
        if tok.type is TokenType.NIL:
            self._advance()
            return ast.NilLit(loc=tok.loc)
        if tok.type is TokenType.LPAREN:
            self._advance()
            expr = self._parse_expr()
            self._expect(TokenType.RPAREN, "')'")
            return expr
        if tok.type is TokenType.NAME:
            self._advance()
            if self._match(TokenType.LPAREN):
                args: list[ast.Expr] = []
                if not self._check(TokenType.RPAREN):
                    while True:
                        args.append(self._parse_expr())
                        if not self._match(TokenType.COMMA):
                            break
                self._expect(TokenType.RPAREN, "')'")
                return ast.Call(loc=tok.loc, name=tok.value, args=args)
            return ast.Var(loc=tok.loc, name=tok.value)
        tok = self._cur()
        label = repr(tok.value) if tok.value else tok.type.name
        raise ParseError(f"unexpected token {label}", tok.loc)


def parse(source: str) -> ast.Program:
    tokens = Lexer(source).tokenize()
    return Parser(tokens).parse_program()
