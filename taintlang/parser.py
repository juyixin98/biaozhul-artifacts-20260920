"""Hand-written recursive-descent parser (tokens -> AST).

Pratt-style expression climbing with precedence:

    ||                     lowest
    &&
    == !=
    < <= > >=
    + -
    * / %
    unary ! -              highest
"""

from __future__ import annotations

from . import ast_nodes as ast
from .errors import ParseError
from .lexer import TokType, Token, tokenize
from .location import Span


class Parser:
    def __init__(self, source: str, tokens: list[Token]):
        self.source = source
        self.tokens = tokens
        self.pos = 0

    # -- token helpers --------------------------------------------------
    @property
    def cur(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 1) -> Token:
        idx = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.type is not TokType.EOF:
            self.pos += 1
        return tok

    def check(self, ttype: TokType) -> bool:
        return self.cur.type is ttype

    def accept(self, ttype: TokType) -> Token | None:
        if self.check(ttype):
            return self.advance()
        return None

    def expect(self, ttype: TokType, what: str) -> Token:
        tok = self.cur
        if tok.type is not ttype:
            raise ParseError(f"expected {what} but found {self._describe(tok)}", tok.span)
        return self.advance()

    @staticmethod
    def _describe(tok: Token) -> str:
        if tok.type is TokType.EOF:
            return "end of file"
        if tok.type is TokType.IDENT:
            return f"identifier '{tok.value}'"
        return f"'{tok.value}'"

    # -- grammar --------------------------------------------------------
    def parse_program(self) -> ast.Program:
        start = self.cur.span.start
        functions: list[ast.FuncDecl] = []
        if self.check(TokType.EOF):
            raise ParseError("program must contain at least one function", self.cur.span)
        while not self.check(TokType.EOF):
            functions.append(self.parse_func())
        end = self.tokens[max(0, self.pos - 1)].span.end
        return ast.Program(Span(start, self.cur.span.end, self.source), tuple(functions))

    def parse_func(self) -> ast.FuncDecl:
        start = self.expect(TokType.FUNC, "'func'").span.start
        name_tok = self.expect(TokType.IDENT, "function name")
        self.expect(TokType.LPAREN, "'('")
        params: list[str] = []
        if not self.check(TokType.RPAREN):
            while True:
                p = self.expect(TokType.IDENT, "parameter name")
                if p.value in params:
                    raise ParseError(f"duplicate parameter '{p.value}'", p.span)
                params.append(p.value)
                if not self.accept(TokType.COMMA):
                    break
        self.expect(TokType.RPAREN, "')'")
        body = self.parse_block()
        span = Span(name_tok.span.start, body.span.end, self.source)
        return ast.FuncDecl(span, name_tok.value, tuple(params), body)

    def parse_block(self) -> ast.Block:
        start = self.expect(TokType.LBRACE, "'{'").span.start
        stmts: list[ast.Stmt] = []
        while not self.check(TokType.RBRACE) and not self.check(TokType.EOF):
            stmts.append(self.parse_stmt())
        end_tok = self.expect(TokType.RBRACE, "'}'")
        return ast.Block(Span(self.cur.span.start, end_tok.span.end, ""), tuple(stmts))

    def parse_stmt(self) -> ast.Stmt:
        start_tok = self.cur
        start = start_tok.span.start

        if self.accept(TokType.VAR):
            name_tok = self.expect(TokType.IDENT, "variable name")
            init = None
            if self.accept(TokType.ASSIGN):
                init = self.parse_expr()
            end = self.expect(TokType.SEMI, "';'").span.end
            return ast.VarDecl(Span(start, end, ""), name_tok.value, init)

        if self.accept(TokType.IF):
            return self.parse_if(start)

        if self.accept(TokType.WHILE):
            return self.parse_while(start)

        if self.accept(TokType.RETURN):
            value = None
            if not self.check(TokType.SEMI):
                value = self.parse_expr()
            end = self.expect(TokType.SEMI, "';'").span.end
            return ast.ReturnStmt(Span(start, end, ""), value)

        # expression statement vs assignment: IDENT '='
        if self.check(TokType.IDENT) and self.peek().type is TokType.ASSIGN:
            name_tok = self.advance()
            self.advance()  # '='
            value = self.parse_expr()
            end = self.expect(TokType.SEMI, "';'").span.end
            return ast.Assign(Span(start, end, ""), name_tok.value, value)

        expr = self.parse_expr()
        end = self.expect(TokType.SEMI, "';'").span.end
        return ast.ExprStmt(Span(start, end, ""), expr)

    def parse_if(self, start) -> ast.IfStmt:
        self.expect(TokType.LPAREN, "'('")
        cond = self.parse_expr()
        self.expect(TokType.RPAREN, "')'")
        then_block = self.parse_block()
        else_block = None
        if self.accept(TokType.ELSE):
            else_block = self.parse_block()
        end = (else_block or then_block).span.end
        return ast.IfStmt(Span(start, end, ""), cond, then_block, else_block)

    def parse_while(self, start) -> ast.WhileStmt:
        self.expect(TokType.LPAREN, "'('")
        cond = self.parse_expr()
        self.expect(TokType.RPAREN, "')'")
        body = self.parse_block()
        return ast.WhileStmt(Span(start, body.span.end, ""), cond, body)

    # -- expressions ----------------------------------------------------
    def parse_expr(self) -> ast.Expr:
        return self.parse_or()

    def parse_or(self) -> ast.Expr:
        left = self.parse_and()
        while self.accept(TokType.OR):
            right = self.parse_and()
            left = ast.Binary(Span(left.span.start, right.span.end, ""), "||", left, right)
        return left

    def parse_and(self) -> ast.Expr:
        left = self.parse_equality()
        while self.accept(TokType.AND):
            right = self.parse_equality()
            left = ast.Binary(Span(left.span.start, right.span.end, ""), "&&", left, right)
        return left

    def parse_equality(self) -> ast.Expr:
        left = self.parse_relational()
        while self.cur.type in (TokType.EQ, TokType.NE):
            op_tok = self.advance()
            right = self.parse_relational()
            left = ast.Binary(Span(left.span.start, right.span.end, ""), op_tok.value, left, right)
        return left

    def parse_relational(self) -> ast.Expr:
        left = self.parse_additive()
        while self.cur.type in (TokType.LT, TokType.LE, TokType.GT, TokType.GE):
            op_tok = self.advance()
            right = self.parse_additive()
            left = ast.Binary(Span(left.span.start, right.span.end, ""), op_tok.value, left, right)
        return left

    def parse_additive(self) -> ast.Expr:
        left = self.parse_multiplicative()
        while self.cur.type in (TokType.PLUS, TokType.MINUS):
            op_tok = self.advance()
            right = self.parse_multiplicative()
            left = ast.Binary(Span(left.span.start, right.span.end, ""), op_tok.value, left, right)
        return left

    def parse_multiplicative(self) -> ast.Expr:
        left = self.parse_unary()
        while self.cur.type in (TokType.STAR, TokType.SLASH, TokType.PERCENT):
            op_tok = self.advance()
            right = self.parse_unary()
            left = ast.Binary(Span(left.span.start, right.span.end, ""), op_tok.value, left, right)
        return left

    def parse_unary(self) -> ast.Expr:
        if self.cur.type in (TokType.BANG, TokType.MINUS):
            op_tok = self.advance()
            operand = self.parse_unary()
            return ast.Unary(Span(op_tok.span.start, operand.span.end, ""), op_tok.value, operand)
        return self.parse_call()

    def parse_call(self) -> ast.Expr:
        expr = self.parse_primary()
        # Only a bare IDENT primary may be called (no chained/method calls).
        if isinstance(expr, ast.VarRef) and self.check(TokType.LPAREN):
            lparen = self.advance()
            args: list[ast.Expr] = []
            if not self.check(TokType.RPAREN):
                while True:
                    args.append(self.parse_expr())
                    if not self.accept(TokType.COMMA):
                        break
            rparen = self.expect(TokType.RPAREN, "')'")
            return ast.Call(Span(expr.span.start, rparen.span.end, ""), expr.name, tuple(args))
        return expr

    def parse_primary(self) -> ast.Expr:
        tok = self.cur
        if tok.type is TokType.NUMBER:
            self.advance()
            return ast.NumberLit(tok.span, tok.value)
        if tok.type is TokType.STRING:
            self.advance()
            return ast.StringLit(tok.span, tok.value)
        if tok.type in (TokType.TRUE, TokType.FALSE):
            self.advance()
            return ast.BoolLit(tok.span, tok.type is TokType.TRUE)
        if tok.type is TokType.IDENT:
            self.advance()
            return ast.VarRef(tok.span, tok.value)
        if tok.type is TokType.LPAREN:
            self.advance()
            expr = self.parse_expr()
            self.expect(TokType.RPAREN, "')'")
            return expr
        raise ParseError(f"expected an expression but found {self._describe(tok)}", tok.span)


def parse(source: str) -> ast.Program:
    """Tokenize and parse ``source``; raises LexError/ParseError on failure."""
    return Parser(source, tokenize(source)).parse_program()
