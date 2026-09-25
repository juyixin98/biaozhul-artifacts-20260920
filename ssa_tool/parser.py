"""ToyLang 递归下降语法分析器。

文法（详见 README「语言参考」）：

    program   := "func" IDENT "(" [param ("," param)*] ")" block
    block     := "{" stmt* "}"
    stmt      := "var" IDENT ["=" expr] ";"
               | IDENT "=" expr ";"
               | "if" "(" expr ")" block ("else" block)?
               | "while" "(" expr ")" block
               | "return" [expr] ";"
               | expr ";"
    expr      := logic_or
    logic_or  := logic_and ("||" logic_and)*
    logic_and := equality ("&&" equality)*
    equality  := relational (("=="|"!=") relational)*
    relational:= additive (("<"|"<="|">"|">=") additive)*
    additive  := mul (("+"|"-") mul)*
    mul       := unary (("*"|"/"|"%") unary)*
    unary     := ("-"|"!") unary | primary
    primary   := INT | "true" | "false" | IDENT | "(" expr ")"
"""

from __future__ import annotations

from . import ast_nodes as ast
from .errors import ParseError
from .lexer import Token, tokenize


class Parser:
    def __init__(self, tokens: list[Token], source: str, file: str):
        self.toks = tokens
        self.src = source
        self.file = file
        self.pos = 0
        self.scopes: list[set[str]] = []

    # ================= 基础工具 =================

    def _peek(self, off: int = 0) -> Token:
        j = self.pos + off
        if j < len(self.toks):
            return self.toks[j]
        return self.toks[-1]

    def _at_end(self) -> bool:
        return self._peek().kind == "EOF"

    def _advance(self) -> Token:
        tok = self._peek()
        if tok.kind != "EOF":
            self.pos += 1
        return tok

    def _check_op(self, value: str) -> bool:
        t = self._peek()
        return t.kind == "OP" and t.value == value

    def _check_kw(self, value: str) -> bool:
        t = self._peek()
        return t.kind == "KEYWORD" and t.value == value

    def _match_op(self, value: str) -> bool:
        if self._check_op(value):
            self._advance()
            return True
        return False

    def _expect_op(self, value: str) -> Token:
        t = self._peek()
        if t.kind != "OP" or t.value != value:
            raise ParseError(f"期望 {value!r}，但遇到 {self._show(t)!r}", t.line, t.col)
        return self._advance()

    def _expect_kw(self, value: str) -> Token:
        t = self._peek()
        if t.kind != "KEYWORD" or t.value != value:
            raise ParseError(f"期望关键字 {value!r}，但遇到 {self._show(t)!r}", t.line, t.col)
        return self._advance()

    @staticmethod
    def _show(t: Token) -> str:
        if t.kind == "EOF":
            return "文件结束"
        return t.value

    def _span(self, first: Token, last: Token) -> ast.Span:
        return ast.Span(
            self.file,
            first.offset,
            last.offset + len(last.value),
            first.line, first.col,
            last.line,
            last.col + max(len(last.value), 1) - 1,
        )

    # ================= 作用域 =================

    def _declare(self, name: str, tok: Token) -> None:
        for scope in self.scopes:
            if name in scope:
                raise ParseError(f"变量 {name!r} 重复声明", tok.line, tok.col)
        self.scopes[-1].add(name)

    def _check_declared(self, name: str, tok: Token) -> None:
        for scope in reversed(self.scopes):
            if name in scope:
                return
        raise ParseError(f"变量 {name!r} 未经声明", tok.line, tok.col)

    # ================= 入口 =================

    def parse_program(self) -> ast.Program:
        first = self._peek()
        func = self.parse_function()
        if not self._at_end():
            t = self._peek()
            raise ParseError(f"顶层只允许一个函数定义，遇到多余记号 {self._show(t)!r}",
                             t.line, t.col)
        span = self._span(first, self._peek())
        return ast.Program(functions=[func], span=span)

    def parse_function(self) -> ast.Function:
        first = self._expect_kw("func")
        name_tok = self._peek()
        if name_tok.kind != "IDENT":
            raise ParseError(f"函数名应为标识符，遇到 {self._show(name_tok)!r}",
                             name_tok.line, name_tok.col)
        self._advance()
        if name_tok.value != "main":
            raise ParseError("本工具链仅支持单个入口函数，名称必须为 main",
                             name_tok.line, name_tok.col)

        self._expect_op("(")
        params: list[ast.FuncParam] = []
        self.scopes.append(set())  # 参数作用域
        if not self._check_op(")"):
            while True:
                p = self._peek()
                if p.kind != "IDENT":
                    raise ParseError(f"参数名应为标识符，遇到 {self._show(p)!r}",
                                     p.line, p.col)
                self._advance()
                self._declare(p.value, p)
                params.append(ast.FuncParam(p.value, self._span(p, p)))
                if not self._match_op(","):
                    break
        self._expect_op(")")
        body = self.parse_block(own_scope=False)
        last = self.toks[self.pos - 1]
        return ast.Function(name=name_tok.value, params=params, body=body,
                            span=self._span(first, last),
                            name_span=self._span(name_tok, name_tok))

    def parse_block(self, own_scope: bool = True) -> list[ast.Stmt]:
        lbrace = self._expect_op("{")
        if own_scope:
            self.scopes.append(set())
        stmts: list[ast.Stmt] = []
        while not self._check_op("}"):
            if self._at_end():
                raise ParseError("缺少与 { 配对的 }", lbrace.line, lbrace.col)
            stmts.append(self.parse_stmt())
        rbrace = self._advance()
        if own_scope:
            self.scopes.pop()
        return stmts  # span 由调用方按需构造；rbrace 已消费

    # ================= 语句 =================

    def parse_stmt(self) -> ast.Stmt:
        t = self._peek()
        if t.kind == "KEYWORD":
            if t.value == "var":
                return self.parse_var_decl()
            if t.value == "if":
                return self.parse_if()
            if t.value == "while":
                return self.parse_while()
            if t.value == "return":
                return self.parse_return()
            if t.value in ("else",):
                raise ParseError("没有匹配 if 的 else", t.line, t.col)
        # 赋值：IDENT "=" ... （仅向前看一个记号）
        if t.kind == "IDENT" and self._peek(1).kind == "OP" and self._peek(1).value == "=":
            return self.parse_assign()
        return self.parse_expr_stmt()

    def parse_var_decl(self) -> ast.VarDecl:
        kw = self._expect_kw("var")
        name_tok = self._peek()
        if name_tok.kind != "IDENT":
            raise ParseError(f"var 后应为标识符，遇到 {self._show(name_tok)!r}",
                             name_tok.line, name_tok.col)
        self._advance()
        self._declare(name_tok.value, name_tok)
        init = None
        semi = name_tok
        if self._match_op("="):
            init = self.parse_expr()
            semi = self.toks[self.pos - 1]
        self._expect_semi_after(semi)
        return ast.VarDecl(name=name_tok.value, init=init,
                           span=self._span(kw, self._prev_meaningful()),
                           name_span=self._span(name_tok, name_tok))

    def parse_assign(self) -> ast.Assign:
        name_tok = self._advance()
        self._check_declared(name_tok.value, name_tok)
        eq = self._expect_op("=")
        value = self.parse_expr()
        self._expect_semi_after(eq)
        return ast.Assign(name=name_tok.value, value=value,
                          span=self._span(name_tok, self._prev_meaningful()),
                          name_span=self._span(name_tok, name_tok))

    def parse_if(self) -> ast.If:
        kw = self._expect_kw("if")
        self._expect_op("(")
        cond = self.parse_expr()
        cond_last = self.toks[self.pos - 1]
        self._expect_op(")")
        then_body = self.parse_block()
        else_body = None
        last_tok = self.toks[self.pos - 1]
        if self._check_kw("else"):
            self._advance()
            if self._check_kw("if"):
                else_body = [self.parse_if()]
            else:
                else_body = self.parse_block()
            last_tok = self.toks[self.pos - 1]
        return ast.If(cond=cond, then_body=then_body, else_body=else_body,
                      span=self._span(kw, last_tok),
                      cond_span=self._span(self.tok_at_span_start(cond), cond_last))

    def parse_while(self) -> ast.While:
        kw = self._expect_kw("while")
        self._expect_op("(")
        cond = self.parse_expr()
        cond_last = self.toks[self.pos - 1]
        self._expect_op(")")
        body = self.parse_block()
        last_tok = self.toks[self.pos - 1]
        return ast.While(cond=cond, body=body,
                         span=self._span(kw, last_tok),
                         cond_span=self._span(self.tok_at_span_start(cond), cond_last))

    def parse_return(self) -> ast.Return:
        kw = self._expect_kw("return")
        value = None
        value_span = None
        if not self._check_op(";"):
            value = self.parse_expr()
            value_span = value.span
        self._expect_op(";")
        last = self.toks[self.pos - 2]
        return ast.Return(value=value, value_span=value_span,
                          span=self._span(kw, last))

    def parse_expr_stmt(self) -> ast.ExprStmt:
        first_tok = self._peek()
        expr = self.parse_expr()
        semi = self._expect_semi_after(first_tok)
        return ast.ExprStmt(expr=expr, span=self._span(
            self.tok_at_span_start(expr), semi))

    def _expect_semi_after(self, before: Token) -> Token:
        t = self._peek()
        if t.kind != "OP" or t.value != ";":
            raise ParseError(f"语句末尾应为 ';'，遇到 {self._show(t)!r}", t.line, t.col)
        return self._advance()

    def _prev_meaningful(self) -> Token:
        return self.toks[max(self.pos - 2, 0)]

    def tok_at_span_start(self, expr: ast.Expr) -> Token:
        # 表达式 span 起点 token —— 用二分偏移简单线性查找即可（程序很小）
        for tok in self.toks:
            if tok.offset == expr.span.start_off:
                return tok
        return self.toks[0]

    # ================= 表达式（优先级爬升） =================

    def parse_expr(self) -> ast.Expr:
        return self.parse_logic_or()

    def _binary_level(self, parse_sub, ops: tuple[str, ...]):
        left = parse_sub()
        while True:
            t = self._peek()
            if t.kind == "OP" and t.value in ops:
                self._advance()
                right = parse_sub()
                left = ast.Binary(op=t.value, left=left, right=right,
                                  span=self._span(self.tok_at_span_start(left),
                                                  self.toks[self.pos - 1]))
            else:
                return left

    def parse_logic_or(self):
        return self._binary_level(self.parse_logic_and, ("||",))

    def parse_logic_and(self):
        return self._binary_level(self.parse_equality, ("&&",))

    def parse_equality(self):
        return self._binary_level(self.parse_relational, ("==", "!="))

    def parse_relational(self):
        return self._binary_level(self.parse_additive, ("<", "<=", ">", ">="))

    def parse_additive(self):
        return self._binary_level(self.parse_mul, ("+", "-"))

    def parse_mul(self):
        return self._binary_level(self.parse_unary, ("*", "/", "%"))

    def parse_unary(self):
        t = self._peek()
        if t.kind == "OP" and t.value in ("-", "!"):
            self._advance()
            operand = self.parse_unary()
            return ast.Unary(op=t.value, operand=operand,
                             span=self._span(t, self.toks[self.pos - 1]))
        return self.parse_primary()

    def parse_primary(self) -> ast.Expr:
        t = self._peek()
        if t.kind == "INT":
            self._advance()
            return ast.IntLit(value=int(t.value), span=self._span(t, t))
        if t.kind == "KEYWORD" and t.value in ("true", "false"):
            self._advance()
            return ast.BoolLit(value=(t.value == "true"), span=self._span(t, t))
        if t.kind == "IDENT":
            self._advance()
            self._check_declared(t.value, t)
            return ast.VarRef(name=t.value, span=self._span(t, t))
        if self._check_op("("):
            self._advance()
            expr = self.parse_expr()
            self._expect_op(")")
            return expr
        raise ParseError(f"表达式中出现意外记号 {self._show(t)!r}", t.line, t.col)


def parse(source: str, file: str = "<src>") -> ast.Program:
    tokens = tokenize(source, file)
    return Parser(tokens, source, file).parse_program()
