"""Recursive-descent parser.

Grammar (semicolons terminate simple statements; `var` is optional):

    program     := function+
    function    := "func" NAME "(" params? ")" "{" stmt* "}"
    params      := NAME ("," NAME)*
    stmt        := "var" NAME ("=" expr)? ";"
                 | NAME "=" expr ";"
                 | "return" expr? ";"
                 | "if" "(" expr ")" block ("else" block)?
                 | "while" "(" expr ")" block
                 | expr ";"
    block       := "{" stmt* "}"
    expr        := or_expr
    or_expr     := and_expr ("or" and_expr)*
    and_expr    := not_expr ("and" not_expr)*
    not_expr    := ("not" | "!") not_expr | comparison
    comparison  := additive (("==" | "!=" | "<" | "<=" | ">" | ">=") additive)*
    additive    := term (("+" | "-") term)*
    term        := factor (("*" | "/" | "%") factor)*
    factor      := ("-" | "!") factor | primary
    primary     := INTEGER | STRING | "true" | "false" | "nil"
                 | NAME "(" args? ")" | NAME | "(" expr ")"
"""

from __future__ import annotations

from .ast_nodes import (
    Assign,
    BinaryOp,
    BoolLit,
    Call,
    Expr,
    ExprStmt,
    FuncDecl,
    IfStmt,
    IntLit,
    Location,
    NilLit,
    Program,
    ReturnStmt,
    StrLit,
    Stmt,
    UnaryOp,
    VarDecl,
    Variable,
    WhileStmt,
)
from .lexer import Lexer, Token, TokenType


class ParseError(Exception):
    pass


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.i = 0

    # -- token helpers -----------------------------------------------------
    def _peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.i + offset, len(self.tokens) - 1)]

    def _check(self, ttype: TokenType) -> bool:
        return self._peek().type is ttype

    def _accept(self, *types: TokenType) -> Token | None:
        if self._peek().type in types:
            tok = self._peek()
            self.i += 1
            return tok
        return None

    def _expect(self, ttype: TokenType, what: str) -> Token:
        tok = self._peek()
        if tok.type is not ttype:
            shown = tok.value or tok.type.name
            raise ParseError(
                f"expected {what} but found {shown!r} "
                f"at line {tok.line}, column {tok.col}"
            )
        self.i += 1
        return tok

    # -- entry -------------------------------------------------------------
    def parse_program(self, entry: str = "main") -> Program:
        functions: dict[str, FuncDecl] = {}
        order: list[str] = []
        while not self._check(TokenType.EOF):
            fn = self._parse_function()
            if fn.name in functions:
                raise ParseError(
                    f"duplicate function {fn.name!r} at line {fn.loc.line}"
                )
            functions[fn.name] = fn
            order.append(fn.name)
        if not functions:
            raise ParseError("program contains no functions")
        if entry not in functions:
            raise ParseError(
                f"entry function {entry!r} not found "
                f"(available: {', '.join(order)})"
            )
        return Program(functions=functions, order=order, entry=entry)

    def _parse_function(self) -> FuncDecl:
        loc_tok = self._expect(TokenType.FUNC, "'func'")
        name_tok = self._expect(TokenType.NAME, "function name")
        self._expect(TokenType.LPAREN, "'('")
        params: list[str] = []
        param_locs: list[Location] = []
        if not self._check(TokenType.RPAREN):
            while True:
                p = self._expect(TokenType.NAME, "parameter name")
                if p.value in params:
                    raise ParseError(
                        f"duplicate parameter {p.value!r} at line {p.line}"
                    )
                params.append(p.value)
                param_locs.append(Location(p.line, p.col))
                if not self._accept(TokenType.COMMA):
                    break
        self._expect(TokenType.RPAREN, "')'")
        self._expect(TokenType.LBRACE, "'{'")
        body = self._parse_stmts_until(TokenType.RBRACE)
        self._expect(TokenType.RBRACE, "'}'")
        return FuncDecl(
            name=name_tok.value,
            params=params,
            body=body,
            loc=Location(loc_tok.line, loc_tok.col),
            param_locs=param_locs,
        )

    # -- statements --------------------------------------------------------
    def _parse_block(self) -> list[Stmt]:
        self._expect(TokenType.LBRACE, "'{'")
        stmts = self._parse_stmts_until(TokenType.RBRACE)
        self._expect(TokenType.RBRACE, "'}'")
        return stmts

    def _parse_stmts_until(self, terminator: TokenType) -> list[Stmt]:
        stmts: list[Stmt] = []
        while not self._check(terminator) and not self._check(TokenType.EOF):
            stmts.append(self._parse_stmt())
        return stmts

    def _parse_stmt(self) -> Stmt:
        tok = self._peek()
        if tok.type is TokenType.VAR:
            self.i += 1
            name = self._expect(TokenType.NAME, "variable name")
            init: Expr | None = None
            if self._accept(TokenType.ASSIGN):
                init = self._parse_expr()
            self._expect(TokenType.SEMI, "';'")
            return VarDecl(name.value, init, Location(tok.line, tok.col))
        if tok.type is TokenType.RETURN:
            self.i += 1
            value = None
            if not self._check(TokenType.SEMI):
                value = self._parse_expr()
            self._expect(TokenType.SEMI, "';'")
            return ReturnStmt(value, Location(tok.line, tok.col))
        if tok.type is TokenType.IF:
            return self._parse_if()
        if tok.type is TokenType.WHILE:
            return self._parse_while()
        # expression statement or plain assignment (NAME "=" expr)
        start = self.i
        expr = self._parse_expr()
        if self._accept(TokenType.ASSIGN):
            if not isinstance(expr, Variable):
                raise ParseError(
                    f"invalid assignment target at line {tok.line}, column {tok.col}"
                )
            value = self._parse_expr()
            self._expect(TokenType.SEMI, "';'")
            return Assign(expr.name, value, Location(tok.line, tok.col))
        self._expect(TokenType.SEMI, "';'")
        # consume-but-discard guard: `var` already handled; start kept for clarity
        del start
        return ExprStmt(expr, Location(tok.line, tok.col))

    def _parse_if(self) -> IfStmt:
        tok = self._expect(TokenType.IF, "'if'")
        self._expect(TokenType.LPAREN, "'('")
        cond = self._parse_expr()
        self._expect(TokenType.RPAREN, "')'")
        then_body = self._parse_block()
        else_body: list[Stmt] = []
        if self._accept(TokenType.ELSE):
            if self._check(TokenType.IF):
                else_body = [self._parse_if()]
            else:
                else_body = self._parse_block()
        return IfStmt(cond, then_body, else_body, Location(tok.line, tok.col))

    def _parse_while(self) -> WhileStmt:
        tok = self._expect(TokenType.WHILE, "'while'")
        self._expect(TokenType.LPAREN, "'('")
        cond = self._parse_expr()
        self._expect(TokenType.RPAREN, "')'")
        body = self._parse_block()
        return WhileStmt(cond, body, Location(tok.line, tok.col))

    # -- expressions -------------------------------------------------------
    def _parse_expr(self) -> Expr:
        return self._parse_or()

    def _parse_or(self) -> Expr:
        left = self._parse_and()
        while self._accept(TokenType.OR):
            op_tok = self.tokens[self.i - 1]
            right = self._parse_and()
            left = BinaryOp("or", left, right, Location(op_tok.line, op_tok.col))
        return left

    def _parse_and(self) -> Expr:
        left = self._parse_not()
        while self._accept(TokenType.AND):
            op_tok = self.tokens[self.i - 1]
            right = self._parse_not()
            left = BinaryOp("and", left, right, Location(op_tok.line, op_tok.col))
        return left

    def _parse_not(self) -> Expr:
        tok = self._accept(TokenType.NOT, TokenType.BANG)
        if tok is not None:
            operand = self._parse_not()
            return UnaryOp("not", operand, Location(tok.line, tok.col))
        return self._parse_comparison()

    _CMP = {
        TokenType.EQ: "==",
        TokenType.NE: "!=",
        TokenType.LT: "<",
        TokenType.LE: "<=",
        TokenType.GT: ">",
        TokenType.GE: ">=",
    }

    def _parse_comparison(self) -> Expr:
        left = self._parse_additive()
        while self._peek().type in self._CMP:
            op_tok = self.tokens[self.i]
            self.i += 1
            right = self._parse_additive()
            left = BinaryOp(
                self._CMP[op_tok.type], left, right,
                Location(op_tok.line, op_tok.col),
            )
        return left

    def _binops(
        self, next_level: "callable", ops: dict[TokenType, str]
    ) -> Expr:
        left = next_level()
        while self._peek().type in ops:
            op_tok = self.tokens[self.i]
            self.i += 1
            right = next_level()
            left = BinaryOp(
                ops[op_tok.type], left, right, Location(op_tok.line, op_tok.col)
            )
        return left

    def _parse_additive(self) -> Expr:
        return self._binops(
            self._parse_term,
            {TokenType.PLUS: "+", TokenType.MINUS: "-"},
        )

    def _parse_term(self) -> Expr:
        return self._binops(
            self._parse_factor,
            {TokenType.STAR: "*", TokenType.SLASH: "/", TokenType.PERCENT: "%"},
        )

    def _parse_factor(self) -> Expr:
        tok = self._accept(TokenType.MINUS, TokenType.BANG)
        if tok is not None:
            operand = self._parse_factor()
            return UnaryOp("-" if tok.type is TokenType.MINUS else "not",
                           operand, Location(tok.line, tok.col))
        return self._parse_primary()

    def _parse_primary(self) -> Expr:
        tok = self._peek()
        if tok.type is TokenType.INTEGER:
            self.i += 1
            return IntLit(int(tok.value), Location(tok.line, tok.col))
        if tok.type is TokenType.STRING:
            self.i += 1
            return StrLit(tok.value, Location(tok.line, tok.col))
        if tok.type in (TokenType.TRUE, TokenType.FALSE):
            self.i += 1
            return BoolLit(tok.type is TokenType.TRUE, Location(tok.line, tok.col))
        if tok.type is TokenType.NIL:
            self.i += 1
            return NilLit(Location(tok.line, tok.col))
        if tok.type is TokenType.LPAREN:
            self.i += 1
            expr = self._parse_expr()
            self._expect(TokenType.RPAREN, "')'")
            return expr
        if tok.type is TokenType.NAME:
            self.i += 1
            if self._accept(TokenType.LPAREN):
                args: list[Expr] = []
                if not self._check(TokenType.RPAREN):
                    while True:
                        args.append(self._parse_expr())
                        if not self._accept(TokenType.COMMA):
                            break
                self._expect(TokenType.RPAREN, "')'")
                return Call(tok.value, args, Location(tok.line, tok.col))
            return Variable(tok.value, Location(tok.line, tok.col))
        shown = tok.value or tok.type.name
        raise ParseError(
            f"unexpected token {shown!r} "
            f"at line {tok.line}, column {tok.col}"
        )


def parse(source: str, entry: str = "main") -> Program:
    tokens = Lexer(source).tokenize()
    return Parser(tokens).parse_program(entry=entry)
