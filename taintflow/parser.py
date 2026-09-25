"""递归下降语法分析器。

文法（完整定义见 README）::

    program  := fndef+
    fndef    := 'fn' IDENT '(' params? ')' block
    block    := '{' stmt* '}'
    stmt     := ';'
             | 'return' expr? ';'
             | 'if' '(' expr ')' block ('else' (block | ifstmt))?
             | 'while' '(' expr ')' block
             | expr ';'        （expr 为 Name '=' 时即赋值）

表达式优先级从低到高： ``or``  <  ``and``  <  == !=  <  < > <= >=  <
+ -  <  * / %  <  一元 ! - not  <  调用  <  基本式。仅支持直接函数调用
``f(...)``，不支持方法调用/函数值。
"""

from __future__ import annotations

from typing import List, Optional

from . import ast_nodes as ast
from .errors import ParseError
from .lexer import Token
from .location import Span


# (操作符, 下一级解析方法名)
_BIN_PRECEDENCE = [
    ("||", "_parse_and"),
    ("or", "_parse_and"),
]


class Parser:
    def __init__(self, tokens: List[Token]):
        self.toks = tokens
        self.p = 0

    # ---- Token 游标 ----
    def _peek(self, off: int = 0) -> Token:
        j = self.p + off
        if j < len(self.toks):
            return self.toks[j]
        return self.toks[-1]  # eof

    def _at_end(self) -> bool:
        return self._peek().kind == "eof"

    def _advance(self) -> Token:
        t = self._peek()
        if not self._at_end():
            self.p += 1
        return t

    def _check_op(self, *ops: str) -> bool:
        t = self._peek()
        return t.kind == "op" and t.value in ops

    def _check_kw(self, *kws: str) -> bool:
        t = self._peek()
        return t.kind == "keyword" and t.value in kws

    def _expect_op(self, op: str, what: str = "") -> Token:
        if not self._check_op(op):
            t = self._peek()
            raise ParseError(f"期望 {what or op!r}，但遇到 {self._describe(t)}", t.span)
        return self._advance()

    def _expect_kw(self, kw: str) -> Token:
        if not self._check_kw(kw):
            t = self._peek()
            raise ParseError(f"期望关键字 {kw!r}，但遇到 {self._describe(t)}", t.span)
        return self._advance()

    @staticmethod
    def _describe(t: Token) -> str:
        if t.kind == "eof":
            return "文件结束"
        if t.kind == "keyword":
            return f"关键字 {t.value!r}"
        return f"{t.kind} {t.value!r}"

    # ---- 程序 / 函数 ----
    def parse_program(self) -> ast.Program:
        start_tok = self._peek()
        funcs: List[ast.Function] = []
        while not self._at_end():
            funcs.append(self._parse_function())
        if not funcs:
            raise ParseError("程序至少需要定义一个函数", start_tok.span)
        return ast.Program(funcs, Span(start_tok.span.start, self._peek().span.end))

    def _parse_function(self) -> ast.Function:
        kw = self._expect_kw("fn")
        name_tok = self._expect_ident()
        self._expect_op("(", "'('")
        params: List[str] = []
        if not self._check_op(")"):
            while True:
                pt = self._expect_ident()
                if pt.value in params:
                    raise ParseError(f"参数 {pt.value!r} 重复", pt.span)
                params.append(pt.value)
                if self._check_op(","):
                    self._advance()
                    continue
                break
        self._expect_op(")", "')'")
        body = self._parse_block()
        return ast.Function(name_tok.value, params, body,
                            Span(kw.span.start, body[-1].span.end if body else self._peek().span.end),
                            name_tok.span)

    def _expect_ident(self) -> Token:
        t = self._peek()
        if t.kind != "ident":
            raise ParseError(f"期望标识符，但遇到 {self._describe(t)}", t.span)
        return self._advance()

    def _parse_block(self) -> List[ast.Stmt]:
        self._expect_op("{", "'{'")
        stmts: List[ast.Stmt] = []
        while not self._check_op("}"):
            if self._at_end():
                raise ParseError("缺少 '}' 闭合代码块", self._peek().span)
            stmts.append(self._parse_stmt())
        close = self._expect_op("}", "'}'")
        if not stmts:
            return [ast.ExprStmt(
                Span(close.span.start, close.span.end),
                ast.NumberLit(Span(close.span.start, close.span.end), 0),
            )]
        return stmts

    # ---- 语句 ----
    def _parse_stmt(self) -> ast.Stmt:
        if self._check_op(";"):
            t = self._advance()
            return ast.ExprStmt(t.span, ast.NumberLit(t.span, 0))
        if self._check_kw("return"):
            return self._parse_return()
        if self._check_kw("if"):
            return self._parse_if()
        if self._check_kw("while"):
            return self._parse_while()
        return self._parse_expr_stmt()

    def _parse_return(self) -> ast.Return:
        kw = self._advance()
        value: Optional[ast.Expr] = None
        if not self._check_op(";"):
            value = self._parse_expr()
        semi = self._expect_op(";", "';'")
        return ast.Return(Span(kw.span.start, semi.span.end), value)

    def _parse_if(self) -> ast.If:
        kw = self._advance()
        self._expect_op("(", "'('")
        cond = self._parse_expr()
        self._expect_op(")", "')'")
        then_body = self._parse_block()
        else_body: List[ast.Stmt] = []
        if self._check_kw("else"):
            self._advance()
            if self._check_kw("if"):
                else_body = [self._parse_if()]
            else:
                else_body = self._parse_block()
        end_tok = self._peek()
        return ast.If(Span(kw.span.start, end_tok.span.start),
                      cond, then_body, else_body)

    def _parse_while(self) -> ast.While:
        kw = self._advance()
        self._expect_op("(", "'('")
        cond = self._parse_expr()
        self._expect_op(")", "')'")
        body = self._parse_block()
        return ast.While(kw.span, cond, body)

    def _parse_expr_stmt(self) -> ast.Stmt:
        start_tok = self._peek()
        expr = self._parse_expr()
        if self._check_op("="):
            eq = self._advance()
            if not isinstance(expr, ast.Name):
                raise ParseError("赋值目标必须是简单变量名", expr.span)
            value = self._parse_expr()
            semi = self._expect_op(";", "';'")
            return ast.Assign(Span(start_tok.span.start, semi.span.end),
                              expr.name, value, expr.span)
        semi = self._expect_op(";", "';'")
        return ast.ExprStmt(Span(start_tok.span.start, semi.span.end), expr)

    # ---- 表达式（优先级爬升）----
    def _parse_expr(self) -> ast.Expr:
        return self._parse_or()

    def _parse_binary(self, ops, higher):
        left = higher()
        while self._is_bin_op(ops):
            op_tok = self._advance()
            right = higher()
            left = ast.Binary(Span(left.span.start, right.span.end),
                              op_tok.value, left, right)
        return left

    def _is_bin_op(self, ops) -> bool:
        t = self._peek()
        if t.kind == "op" and t.value in ops:
            return True
        if t.kind == "keyword" and t.value in ops:
            return True
        return False

    def _parse_or(self) -> ast.Expr:
        left = self._parse_and()
        while self._check_op("||") or self._check_kw("or"):
            op_tok = self._advance()
            right = self._parse_and()
            left = ast.Binary(Span(left.span.start, right.span.end),
                              "||" if op_tok.value == "or" else op_tok.value,
                              left, right)
        return left

    def _parse_and(self) -> ast.Expr:
        left = self._parse_equality()
        while self._check_op("&&") or self._check_kw("and"):
            op_tok = self._advance()
            right = self._parse_equality()
            left = ast.Binary(Span(left.span.start, right.span.end),
                              "&&" if op_tok.value == "and" else op_tok.value,
                              left, right)
        return left

    def _parse_equality(self) -> ast.Expr:
        return self._parse_binary(("==", "!="), self._parse_relational)

    def _parse_relational(self) -> ast.Expr:
        return self._parse_binary(("<", ">", "<=", ">="), self._parse_additive)

    def _parse_additive(self) -> ast.Expr:
        return self._parse_binary(("+", "-"), self._parse_multiplicative)

    def _parse_multiplicative(self) -> ast.Expr:
        return self._parse_binary(("*", "/", "%"), self._parse_unary)

    def _parse_unary(self) -> ast.Expr:
        if self._check_op("!", "-") or self._check_kw("not"):
            op_tok = self._advance()
            operand = self._parse_unary()
            op = "!" if op_tok.value == "not" else op_tok.value
            return ast.Unary(Span(op_tok.span.start, operand.span.end), op, operand)
        return self._parse_call_or_primary()

    def _parse_call_or_primary(self) -> ast.Expr:
        t = self._peek()
        if t.kind == "ident" and self._peek(1).kind == "op" and self._peek(1).value == "(":
            name_tok = self._advance()
            self._advance()  # (
            args: List[ast.Expr] = []
            if not self._check_op(")"):
                while True:
                    args.append(self._parse_expr())
                    if self._check_op(","):
                        self._advance()
                        continue
                    break
            close = self._expect_op(")", "')'")
            return ast.Call(Span(name_tok.span.start, close.span.end),
                            name_tok.value, args, name_tok.span)
        return self._parse_primary()

    def _parse_primary(self) -> ast.Expr:
        t = self._peek()
        if t.kind == "number":
            self._advance()
            return ast.NumberLit(t.span, t.value)
        if t.kind == "string":
            self._advance()
            return ast.StringLit(t.span, t.value)
        if t.kind == "keyword" and t.value in ("true", "false"):
            self._advance()
            return ast.BoolLit(t.span, t.value == "true")
        if t.kind == "ident":
            self._advance()
            # ident 后紧跟 '(' 的情形已在 _parse_call_or_primary 处理；
            # 走到这里说明不是调用，按变量名处理。
            if self._check_op("("):
                raise ParseError("不支持函数值/方法调用，仅支持直接调用 f(...)", t.span)
            return ast.Name(t.span, t.value)
        if self._check_op("("):
            self._advance()
            expr = self._parse_expr()
            self._expect_op(")", "')'")
            # 不允许 (expr)(...) 形式的间接调用
            if self._check_op("("):
                raise ParseError("不支持间接调用", self._peek().span)
            return expr
        raise ParseError(f"期望表达式，但遇到 {self._describe(t)}", t.span)
