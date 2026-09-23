"""Hand-written recursive-descent parser for ScL.

Grammar (informal, see README for the documented version)::

    program   := stmt*
    block     := "{" stmt* "}"
    stmt      := let | fn | if | while | return | block | exprStmt
    let       := "let" IDENT ("=" expr)? ";"
    fn        := "fn" IDENT "(" params ")" block
    if        := "if" expr block ("else" (if | block))?
    while     := "while" expr block
    return    := "return" expr? ";"
    exprStmt  := expr ";"

    expr       := or
    or         := and ("or" and)*        (short-circuit)
    and        := equality ("and" equality)*
    equality   := comparison (("==" | "!=") comparison)*
    comparison := additive (("<" | "<=" | ">" | ">=") additive)*
    additive   := factor (("+" | "-") factor)*
    factor     := unary (("*" | "/" | "%") unary)*
    unary      := ("-" | "not") unary | call
    call       := primary ("(" args? ")")*
    primary    := INT | STRING | "true" | "false" | "nil"
                | IDENT ("=" expr)?
                | "(" expr ")"
                | "fn" "(" params ")" block
                | "print" "(" args? ")"

Only a bare ``IDENT`` may appear on the left of ``=`` (no compound
assignment). ``and``/``or`` short-circuit at runtime.
"""

from . import ast_nodes as ast
from .errors import CompileError, Span
from .lexer import Lexer, Token, TokenKind


class Parser:
    def __init__(self, tokens: list[Token], source: str):
        self.tokens = tokens
        self.source = source
        self.i = 0

    # -- token plumbing ---------------------------------------------------

    def _peek(self, off: int = 0) -> Token:
        j = min(self.i + off, len(self.tokens) - 1)
        return self.tokens[j]

    def _check(self, kind: TokenKind) -> bool:
        return self._peek().kind == kind

    def _accept(self, *kinds: TokenKind) -> Token | None:
        if self._peek().kind in kinds:
            tok = self._peek()
            self.i += 1
            return tok
        return None

    def _expect(self, kind: TokenKind, what: str) -> Token:
        tok = self._peek()
        if tok.kind != kind:
            raise CompileError(f"expected {what} but found {self._tok_text(tok)}",
                               tok.span)
        self.i += 1
        return tok

    @staticmethod
    def _tok_text(tok: Token) -> str:
        if tok.kind in (TokenKind.INT, TokenKind.STRING):
            return repr(tok.value)
        if tok.kind == TokenKind.IDENT:
            return f"identifier {tok.value!r}"
        if tok.kind == TokenKind.EOF:
            return "end of input"
        return repr(tok.value)

    # -- entry point ------------------------------------------------------

    def parse(self) -> ast.Program:
        start = self._peek().span
        stmts: list[ast.Stmt] = []
        while not self._check(TokenKind.EOF):
            stmts.append(self._stmt())
        end = self._peek().span
        span = Span(start.start, end.end, start.line, start.col,
                    end.end_line, end.end_col, self.source)
        return ast.Program(body=stmts, span=span)

    # -- statements -------------------------------------------------------

    def _block(self) -> ast.Block:
        open_tok = self._expect(TokenKind.LBRACE, "'{'")
        stmts: list[ast.Stmt] = []
        while not self._check(TokenKind.RBRACE) and not self._check(TokenKind.EOF):
            stmts.append(self._stmt())
        close = self._expect(TokenKind.RBRACE, "'}'")
        return ast.Block(body=stmts, span=self._join(open_tok.span, close.span))

    def _stmt(self) -> ast.Stmt:
        kind = self._peek().kind
        if kind == TokenKind.LET:
            return self._let()
        if kind == TokenKind.FN and self._peek(1).kind == TokenKind.IDENT:
            return self._fn()
        if kind == TokenKind.IF:
            return self._if()
        if kind == TokenKind.WHILE:
            return self._while()
        if kind == TokenKind.RETURN:
            return self._return()
        if kind == TokenKind.LBRACE:
            return self._block()
        return self._expr_stmt()

    def _let(self) -> ast.Let:
        kw = self._accept(TokenKind.LET)
        name_tok = self._expect(TokenKind.IDENT, "variable name")
        init = None
        if self._accept(TokenKind.ASSIGN):
            init = self._expr()
        semi = self._expect(TokenKind.SEMICOLON, "';'")
        return ast.Let(name=name_tok.value, init=init,
                       name_span=name_tok.span,
                       span=self._join(kw.span, semi.span))

    def _fn(self) -> ast.FunctionStmt:
        kw = self._accept(TokenKind.FN)
        name_tok = self._expect(TokenKind.IDENT, "function name")
        params = self._params()
        body = self._block()
        return ast.FunctionStmt(name=name_tok.value, params=params, body=body,
                                name_span=name_tok.span,
                                span=self._join(kw.span, body.span))

    def _params(self) -> list[str]:
        self._expect(TokenKind.LPAREN, "'('")
        params: list[str] = []
        if not self._check(TokenKind.RPAREN):
            while True:
                tok = self._expect(TokenKind.IDENT, "parameter name")
                if tok.value in params:
                    raise CompileError(
                        f"duplicate parameter name {tok.value!r}", tok.span)
                params.append(tok.value)
                if not self._accept(TokenKind.COMMA):
                    break
        self._expect(TokenKind.RPAREN, "')'")
        return params

    def _if(self) -> ast.If:
        kw = self._accept(TokenKind.IF)
        cond = self._expr()
        then = self._block()
        otherwise = None
        if self._accept(TokenKind.ELSE):
            if self._check(TokenKind.IF):
                otherwise = ast.Block(body=[self._if()],
                                      span=self._peek().span)
            else:
                otherwise = self._block()
        return ast.If(cond=cond, then=then, otherwise=otherwise,
                      span=self._join(kw.span, (otherwise or then).span))

    def _while(self) -> ast.While:
        kw = self._accept(TokenKind.WHILE)
        cond = self._expr()
        body = self._block()
        return ast.While(cond=cond, body=body,
                         span=self._join(kw.span, body.span))

    def _return(self) -> ast.Return:
        kw = self._accept(TokenKind.RETURN)
        value = None
        if not self._check(TokenKind.SEMICOLON):
            value = self._expr()
        semi = self._expect(TokenKind.SEMICOLON, "';'")
        return ast.Return(value=value, span=self._join(kw.span, semi.span))

    def _expr_stmt(self) -> ast.ExprStmt:
        expr = self._expr()
        semi = self._expect(TokenKind.SEMICOLON, "';'")
        return ast.ExprStmt(expr=expr, span=self._join(expr.span, semi.span))

    # -- expressions ------------------------------------------------------

    def _expr(self) -> ast.Expr:
        return self._parse_or()

    def _bin_chain(self, higher, kinds: dict[TokenKind, str], node):
        left = higher()
        while self._peek().kind in kinds:
            op_tok = self._accept(self._peek().kind)
            right = higher()
            left = node(op=kinds[op_tok.kind], left=left, right=right,
                        op_span=op_tok.span,
                        span=self._join(left.span, right.span))
        return left

    def _parse_or(self) -> ast.Expr:
        left = self._parse_and()
        while self._accept(TokenKind.OR):
            op_tok = self.tokens[self.i - 1]
            right = self._parse_and()
            left = ast.Binary(op="or", left=left, right=right,
                              op_span=op_tok.span,
                              span=self._join(left.span, right.span))
        return left

    def _parse_and(self) -> ast.Expr:
        left = self._parse_equality()
        while self._accept(TokenKind.AND):
            op_tok = self.tokens[self.i - 1]
            right = self._parse_equality()
            left = ast.Binary(op="and", left=left, right=right,
                              op_span=op_tok.span,
                              span=self._join(left.span, right.span))
        return left

    def _parse_equality(self) -> ast.Expr:
        return self._bin_chain(
            self._parse_comparison,
            {TokenKind.EQ: "==", TokenKind.NE: "!="},
            ast.Binary)

    def _parse_comparison(self) -> ast.Expr:
        return self._bin_chain(
            self._parse_additive,
            {TokenKind.LT: "<", TokenKind.LE: "<=",
             TokenKind.GT: ">", TokenKind.GE: ">="},
            ast.Binary)

    def _parse_additive(self) -> ast.Expr:
        return self._bin_chain(
            self._parse_factor,
            {TokenKind.PLUS: "+", TokenKind.MINUS: "-"},
            ast.Binary)

    def _parse_factor(self) -> ast.Expr:
        return self._bin_chain(
            self._parse_unary,
            {TokenKind.STAR: "*", TokenKind.SLASH: "/", TokenKind.PERCENT: "%"},
            ast.Binary)

    def _parse_unary(self) -> ast.Expr:
        if self._check(TokenKind.MINUS) or self._check(TokenKind.NOT):
            op_tok = self._accept(self._peek().kind)
            operand = self._parse_unary()
            op = "-" if op_tok.kind == TokenKind.MINUS else "not"
            return ast.Unary(op=op, operand=operand, op_span=op_tok.span,
                             span=self._join(op_tok.span, operand.span))
        return self._parse_call()

    def _parse_call(self) -> ast.Expr:
        expr = self._primary()
        while self._accept(TokenKind.LPAREN):
            open_span = self.tokens[self.i - 1].span
            args: list[ast.Expr] = []
            if not self._check(TokenKind.RPAREN):
                while True:
                    args.append(self._expr())
                    if not self._accept(TokenKind.COMMA):
                        break
            close = self._expect(TokenKind.RPAREN, "')'")
            expr = ast.Call(callee=expr, args=args,
                            span=self._join(expr.span, close.span))
        return expr

    def _primary(self) -> ast.Expr:
        tok = self._peek()
        if tok.kind == TokenKind.INT:
            self.i += 1
            return ast.IntLit(value=tok.value, span=tok.span)
        if tok.kind == TokenKind.STRING:
            self.i += 1
            return ast.StrLit(value=tok.value, span=tok.span)
        if tok.kind in (TokenKind.TRUE, TokenKind.FALSE):
            self.i += 1
            return ast.BoolLit(value=tok.kind == TokenKind.TRUE, span=tok.span)
        if tok.kind == TokenKind.NIL:
            self.i += 1
            return ast.NilLit(span=tok.span)
        if tok.kind == TokenKind.IDENT:
            self.i += 1
            var = ast.Var(name=tok.value, span=tok.span)
            if self._accept(TokenKind.ASSIGN):
                value = self._expr()
                return ast.Assign(target=var, value=value,
                                  span=self._join(tok.span, value.span))
            return var
        if tok.kind == TokenKind.PRINT:
            return self._print()
        if tok.kind == TokenKind.FN:
            return self._fun_expr()
        if tok.kind == TokenKind.LPAREN:
            self.i += 1
            expr = self._expr()
            self._expect(TokenKind.RPAREN, "')'")
            return expr
        raise CompileError(f"expected an expression but found {self._tok_text(tok)}",
                           tok.span)

    def _print(self) -> ast.PrintExpr:
        kw = self._accept(TokenKind.PRINT)
        self._expect(TokenKind.LPAREN, "'(' after print")
        args: list[ast.Expr] = []
        if not self._check(TokenKind.RPAREN):
            while True:
                args.append(self._expr())
                if not self._accept(TokenKind.COMMA):
                    break
        close = self._expect(TokenKind.RPAREN, "')'")
        return ast.PrintExpr(args=args, span=self._join(kw.span, close.span))

    def _fun_expr(self) -> ast.FunExpr:
        kw = self._accept(TokenKind.FN)
        params = self._params()
        body = self._block()
        return ast.FunExpr(params=params, body=body,
                           span=self._join(kw.span, body.span))

    def _join(self, first: Span, last: Span) -> Span:
        return Span(first.start, last.end, first.line, first.col,
                    last.end_line, last.end_col, self.source)


def parse_source(source: str) -> ast.Program:
    """Convenience: lex + parse source text into an AST."""
    tokens = Lexer(source).tokenize()
    return Parser(tokens, source).parse()
