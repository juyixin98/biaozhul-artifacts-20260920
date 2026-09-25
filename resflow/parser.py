"""Hand-written recursive-descent parser.

Consumes tokens produced by :mod:`resflow.lexer` and builds the AST defined in
:mod:`resflow.ast_nodes`. Parsing is explicit (no grammar generator); only
function calls are legal expression statements.
"""
from __future__ import annotations

from typing import List, Optional

from . import ast_nodes as ast
from .errors import ParseError
from .lexer import Lexer, Token, TokenType
from .locations import SourceFile, Span, merge_spans


class Parser:
    def __init__(self, source: SourceFile):
        self.src = source
        self.tokens: List[Token] = Lexer(source).tokenize()
        self.pos = 0

    # ---------- token helpers ----------

    def peek(self, ahead: int = 0) -> Token:
        idx = self.pos + ahead
        if idx >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[idx]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.type is not TokenType.EOF:
            self.pos += 1
        return tok

    def check(self, ttype: TokenType) -> bool:
        return self.peek().type is ttype

    def accept(self, ttype: TokenType) -> Optional[Token]:
        if self.check(ttype):
            return self.advance()
        return None

    def expect(self, ttype: TokenType, what: str) -> Token:
        tok = self.peek()
        if tok.type is not ttype:
            raise ParseError(f"expected {what} but found {self.describe(tok)}", tok.span)
        return self.advance()

    @staticmethod
    def describe(tok: Token) -> str:
        if tok.type is TokenType.EOF:
            return "end of file"
        if tok.type is TokenType.IDENT:
            return f"identifier {tok.text!r}"
        if tok.type in (TokenType.INT, TokenType.STRING):
            return f"literal {tok.text!r}"
        return repr(tok.text)

    # ---------- grammar ----------

    def parse_program(self) -> ast.Program:
        start_span = self.src.span(0, 0)
        functions: List[ast.Function] = []
        while not self.check(TokenType.EOF):
            functions.append(self.parse_function())
        if not functions:
            raise ParseError("program must contain at least one function", self.peek().span)
        end_tok = self.peek()
        whole = Span(self.src.filename, start_span.start, end_tok.span.end)
        return ast.Program(span=whole, functions=functions)

    def parse_function(self) -> ast.Function:
        fun_tok = self.expect(TokenType.FUN, "'fun'")
        name_tok = self.expect(TokenType.IDENT, "function name")
        self.expect(TokenType.LPAREN, "'('")
        params: List[str] = []
        if not self.check(TokenType.RPAREN):
            while True:
                p = self.expect(TokenType.IDENT, "parameter name")
                params.append(p.text)
                if not self.accept(TokenType.COMMA):
                    break
        self.expect(TokenType.RPAREN, "')'")
        throws = self.accept(TokenType.THROWS) is not None
        body = self.parse_block()
        span = merge_spans(fun_tok.span, body.span)
        return ast.Function(span=span, name=name_tok.text, params=params, throws=throws, body=body)

    def parse_block(self) -> ast.Block:
        open_tok = self.expect(TokenType.LBRACE, "'{'")
        statements: List[ast.Stmt] = []
        while not self.check(TokenType.RBRACE) and not self.check(TokenType.EOF):
            statements.append(self.parse_statement())
        close_tok = self.expect(TokenType.RBRACE, "'}'")
        return ast.Block(span=merge_spans(open_tok.span, close_tok.span), statements=statements)

    def parse_statement(self) -> ast.Stmt:
        ttype = self.peek().type
        if ttype is TokenType.LET:
            return self.parse_let()
        if ttype is TokenType.RELEASE:
            return self.parse_release()
        if ttype is TokenType.USE:
            return self.parse_use()
        if ttype is TokenType.RETURN:
            return self.parse_return()
        if ttype is TokenType.THROW:
            return self.parse_throw()
        if ttype is TokenType.IF:
            return self.parse_if()
        if ttype is TokenType.WHILE:
            return self.parse_while()
        if ttype is TokenType.TRY:
            return self.parse_try()
        if ttype is TokenType.IDENT:
            return self.parse_expr_stmt()
        tok = self.peek()
        raise ParseError(f"unexpected {self.describe(tok)} at start of statement", tok.span)

    def parse_let(self) -> ast.Stmt:
        let_tok = self.advance()  # 'let'
        target_tok = self.expect(TokenType.IDENT, "variable name after 'let'")
        self.expect(TokenType.ASSIGN, "'='")
        if self.accept(TokenType.ACQUIRE):
            self.expect(TokenType.LPAREN, "'('")
            kind_tok = self.expect(TokenType.STRING, "resource kind string")
            self.expect(TokenType.RPAREN, "')'")
            semi = self.expect(TokenType.SEMICOLON, "';'")
            return ast.AcquireStmt(
                span=merge_spans(let_tok.span, semi.span),
                target=target_tok.text,
                kind=kind_tok.text,
            )
        call = self.parse_call()
        semi = self.expect(TokenType.SEMICOLON, "';'")
        return ast.LetCallStmt(
            span=merge_spans(let_tok.span, semi.span),
            target=target_tok.text,
            call=call,
        )

    def parse_release(self) -> ast.ReleaseStmt:
        kw = self.advance()
        target = self.expect(TokenType.IDENT, "resource variable after 'release'")
        semi = self.expect(TokenType.SEMICOLON, "';'")
        return ast.ReleaseStmt(span=merge_spans(kw.span, semi.span), target=target.text)

    def parse_use(self) -> ast.UseStmt:
        kw = self.advance()
        self.expect(TokenType.LPAREN, "'('")
        target = self.expect(TokenType.IDENT, "resource variable inside use(...)")
        self.expect(TokenType.RPAREN, "')'")
        semi = self.expect(TokenType.SEMICOLON, "';'")
        return ast.UseStmt(span=merge_spans(kw.span, semi.span), target=target.text)

    def parse_return(self) -> ast.ReturnStmt:
        kw = self.advance()
        value: Optional[ast.Expr] = None
        if not self.check(TokenType.SEMICOLON):
            value = self.parse_expr()
        semi = self.expect(TokenType.SEMICOLON, "';'")
        return ast.ReturnStmt(span=merge_spans(kw.span, semi.span), value=value)

    def parse_throw(self) -> ast.ThrowStmt:
        kw = self.advance()
        msg = self.expect(TokenType.STRING, "error message string after 'throw'")
        semi = self.expect(TokenType.SEMICOLON, "';'")
        return ast.ThrowStmt(span=merge_spans(kw.span, semi.span), message=msg.text)

    def parse_if(self) -> ast.IfStmt:
        kw = self.advance()
        self.expect(TokenType.LPAREN, "'('")
        cond = self.parse_expr()
        self.expect(TokenType.RPAREN, "')'")
        then_block = self.parse_block()
        else_block: Optional[ast.Block] = None
        span_end: Span = then_block.span
        if self.accept(TokenType.ELSE):
            if self.check(TokenType.IF):
                # Desugar 'else if' into a block containing one if statement.
                nested_if = self.parse_if()
                wrapper = ast.Block(span=nested_if.span, statements=[nested_if])
                else_block = wrapper
                span_end = nested_if.span
            else:
                else_block = self.parse_block()
                span_end = else_block.span
        return ast.IfStmt(
            span=merge_spans(kw.span, span_end),
            cond=cond,
            then_block=then_block,
            else_block=else_block,
        )

    def parse_while(self) -> ast.WhileStmt:
        kw = self.advance()
        self.expect(TokenType.LPAREN, "'('")
        cond = self.parse_expr()
        self.expect(TokenType.RPAREN, "')'")
        body = self.parse_block()
        return ast.WhileStmt(span=merge_spans(kw.span, body.span), cond=cond, body=body)

    def parse_try(self) -> ast.TryStmt:
        kw = self.advance()
        try_block = self.parse_block()
        self.expect(TokenType.CATCH, "'catch'")
        self.expect(TokenType.LPAREN, "'('")
        err_var = self.expect(TokenType.IDENT, "catch error variable")
        self.expect(TokenType.RPAREN, "')'")
        catch_block = self.parse_block()
        return ast.TryStmt(
            span=merge_spans(kw.span, catch_block.span),
            try_block=try_block,
            error_var=err_var.text,
            catch_block=catch_block,
        )

    def parse_expr_stmt(self) -> ast.ExprStmt:
        call = self.parse_call()
        semi = self.expect(TokenType.SEMICOLON, "';'")
        return ast.ExprStmt(span=merge_spans(call.span, semi.span), call=call)

    # ---------- expressions ----------

    def parse_expr(self) -> ast.Expr:
        return self.parse_or()

    def parse_or(self) -> ast.Expr:
        left = self.parse_and()
        while self.check(TokenType.OR):
            op_tok = self.advance()
            right = self.parse_and()
            left = ast.BinaryOp(span=merge_spans(left.span, right.span), op=op_tok.text, left=left, right=right)
        return left

    def parse_and(self) -> ast.Expr:
        left = self.parse_unary()
        while self.check(TokenType.AND):
            op_tok = self.advance()
            right = self.parse_unary()
            left = ast.BinaryOp(span=merge_spans(left.span, right.span), op=op_tok.text, left=left, right=right)
        return left

    def parse_unary(self) -> ast.Expr:
        if self.check(TokenType.BANG):
            op_tok = self.advance()
            operand = self.parse_unary()
            return ast.UnaryOp(span=merge_spans(op_tok.span, operand.span), op=op_tok.text, operand=operand)
        return self.parse_primary()

    def parse_primary(self) -> ast.Expr:
        tok = self.peek()
        if tok.type is TokenType.INT:
            self.advance()
            return ast.IntLit(span=tok.span, value=tok.int_value)
        if tok.type is TokenType.STRING:
            self.advance()
            return ast.StrLit(span=tok.span, value=tok.text)
        if tok.type is TokenType.TRUE:
            self.advance()
            return ast.BoolLit(span=tok.span, value=True)
        if tok.type is TokenType.FALSE:
            self.advance()
            return ast.BoolLit(span=tok.span, value=False)
        if tok.type is TokenType.NULL:
            self.advance()
            return ast.NullLit(span=tok.span)
        if tok.type is TokenType.LPAREN:
            self.advance()
            inner = self.parse_expr()
            self.expect(TokenType.RPAREN, "')'")
            return inner
        if tok.type is TokenType.IDENT:
            if self.peek(1).type is TokenType.LPAREN:
                return self.parse_call()
            self.advance()
            return ast.VarRef(span=tok.span, name=tok.text)
        raise ParseError(f"expected expression but found {self.describe(tok)}", tok.span)

    def parse_call(self) -> ast.CallExpr:
        name_tok = self.expect(TokenType.IDENT, "function name")
        self.expect(TokenType.LPAREN, "'('")
        args: List[ast.Expr] = []
        if not self.check(TokenType.RPAREN):
            while True:
                args.append(self.parse_expr())
                if not self.accept(TokenType.COMMA):
                    break
        close = self.expect(TokenType.RPAREN, "')'")
        return ast.CallExpr(
            span=merge_spans(name_tok.span, close.span),
            name=name_tok.text,
            args=args,
        )


def parse_source(source: SourceFile) -> ast.Program:
    return Parser(source).parse_program()
