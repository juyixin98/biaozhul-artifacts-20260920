"""手写递归下降解析器，构造带源码位置的 AST。"""

from __future__ import annotations

from . import ast as A
from .lexer import Token
from .source import ParseError, SourceText


class Parser:
    def __init__(self, tokens: list[Token], source: SourceText):
        self.toks = tokens
        self.src = source
        self.pos = 0

    # ---------- token 游标原语 ----------

    def _peek(self, off: int = 0) -> Token:
        return self.toks[min(self.pos + off, len(self.toks) - 1)]

    def _advance(self) -> Token:
        tok = self.toks[self.pos]
        if self.pos < len(self.toks) - 1:
            self.pos += 1
        return tok

    def _check(self, text: str) -> bool:
        return self._peek().text == text

    def _accept(self, text: str) -> bool:
        if self._check(text):
            self._advance()
            return True
        return False

    def _expect(self, text: str) -> Token:
        if not self._check(text):
            tok = self._peek()
            sp = self.src.span(tok.start, tok.end)
            raise ParseError(f"expected {text!r} but got {tok.text!r}", sp, self.src)
        return self._advance()

    def _span_to(self, start: int, tok: Token | None = None) -> A.Span:
        end = tok.end if tok is not None else self._peek().start
        return self.src.span(start, end)

    # ---------- 入口 ----------

    def parse_program(self) -> A.Program:
        start = self._peek().start
        stmts: list[A.Stmt] = []
        while self._peek().kind != "EOF":
            stmts.append(self.parse_stmt())
        return A.Program(self.src.span(start, self._peek().start), stmts)

    # ---------- 语句 ----------

    def parse_stmt(self) -> A.Stmt:
        tok = self._peek()
        if tok.text == "{":
            return self.parse_block()
        if tok.text == "if":
            return self.parse_if()
        if tok.text == "while":
            return self.parse_while()
        if tok.text == "print":
            return self.parse_print()
        if tok.kind == "KEYWORD":
            sp = self.src.span(tok.start, tok.end)
            raise ParseError(f"unexpected keyword {tok.text!r}", sp, self.src)
        # 赋值：IDENT '=' expr ';'
        start = tok.start
        name_tok = self._expect_ident()
        self._expect("=")
        value = self.parse_expr()
        semi = self._expect(";")
        return A.Assign(self.src.span(start, semi.end), name_tok.text, value)

    def _expect_ident(self) -> Token:
        tok = self._peek()
        if tok.kind != "IDENT":
            sp = self.src.span(tok.start, tok.end)
            raise ParseError(f"expected identifier but got {tok.text!r}", sp, self.src)
        return self._advance()

    def parse_block(self) -> A.Block:
        lb = self._expect("{")
        body: list[A.Stmt] = []
        while self._peek().kind != "EOF" and not self._check("}"):
            body.append(self.parse_stmt())
        rb = self._expect("}")
        return A.Block(self.src.span(lb.start, rb.end), body)

    def parse_if(self) -> A.If:
        kw = self._expect("if")
        cond = self.parse_expr()
        then = self.parse_stmt()
        otherwise = None
        end_tok = None
        if self._accept("else"):
            otherwise = self.parse_stmt()
            end_tok = self.toks[self.pos - 1]
        return A.If(self._span_to(kw.start, end_tok if otherwise else None),
                    cond, then, otherwise)

    def parse_while(self) -> A.While:
        kw = self._expect("while")
        cond = self.parse_expr()
        body = self.parse_stmt()
        return A.While(self._span_to(kw.start), cond, body)

    def parse_print(self) -> A.Print:
        kw = self._expect("print")
        value = self.parse_expr()
        semi = self._expect(";")
        return A.Print(self.src.span(kw.start, semi.end), value)

    # ---------- 表达式（优先级爬升） ----------

    def parse_expr(self) -> A.Expr:
        return self.parse_or()

    def parse_or(self) -> A.Expr:
        start = self._peek().start
        left = self.parse_and()
        while self._check("||"):
            op = self._advance()
            right = self.parse_and()
            left = A.Logical(self.src.span(start, self._peek().start),
                             op.text, left, right)
        return left

    def parse_and(self) -> A.Expr:
        start = self._peek().start
        left = self.parse_cmp()
        while self._check("&&"):
            op = self._advance()
            right = self.parse_cmp()
            left = A.Logical(self.src.span(start, self._peek().start),
                             op.text, left, right)
        return left

    _CMP_OPS = ("==", "!=", "<", ">", "<=", ">=")

    def parse_cmp(self) -> A.Expr:
        start = self._peek().start
        left = self.parse_add()
        if self._peek().text in self._CMP_OPS:
            op = self._advance()
            right = self.parse_add()
            return A.Binary(self.src.span(start, self._peek().start),
                            op.text, left, right)
        return left

    def parse_add(self) -> A.Expr:
        start = self._peek().start
        left = self.parse_mul()
        while self._peek().text in ("+", "-"):
            op = self._advance()
            right = self.parse_mul()
            left = A.Binary(self.src.span(start, self._peek().start),
                            op.text, left, right)
        return left

    def parse_mul(self) -> A.Expr:
        start = self._peek().start
        left = self.parse_unary()
        while self._peek().text in ("*", "/", "%"):
            op = self._advance()
            right = self.parse_unary()
            left = A.Binary(self.src.span(start, self._peek().start),
                            op.text, left, right)
        return left

    def parse_unary(self) -> A.Expr:
        tok = self._peek()
        if tok.text in ("-", "!"):
            self._advance()
            operand = self.parse_unary()
            return A.Unary(self.src.span(tok.start, self._peek().start),
                           tok.text, operand)
        return self.parse_primary()

    def parse_primary(self) -> A.Expr:
        tok = self._peek()
        if tok.kind == "INT":
            self._advance()
            try:
                value = int(tok.text)
            except ValueError:
                from .source import ParseError as PE
                raise PE(f"integer literal too large: {tok.text!r}",
                         self.src.span(tok.start, tok.end), self.src)
            return A.IntLit(self.src.span(tok.start, tok.end), value)
        if tok.text == "true" or tok.text == "false":
            self._advance()
            return A.BoolLit(self.src.span(tok.start, tok.end), tok.text == "true")
        if tok.kind == "IDENT":
            self._advance()
            return A.Var(self.src.span(tok.start, tok.end), tok.text)
        if tok.text == "(":
            self._advance()
            expr = self.parse_expr()
            self._expect(")")
            return expr
        sp = self.src.span(tok.start, tok.end)
        raise ParseError(f"unexpected token {tok.text!r}", sp, self.src)


def parse_source(source: SourceText) -> A.Program:
    """词法 + 语法分析的便捷入口。"""
    from .lexer import Lexer

    tokens = Lexer(source).tokenize()
    return Parser(tokens, source).parse_program()
