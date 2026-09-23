"""手写递归下降解析器。

语法（VLang，详见 README）::

    program    := func+
    func       := type IDENT "(" params? ")" block
    block      := "{" stmt* "}"
    stmt       := var_decl | assign_or_call ";" | if | while | return
    expr       := 逻辑或表达式
"""

from __future__ import annotations

from . import ast_nodes as ast
from .common import PHASE_PARSER, ToolError
from .lexer import T_EOF, T_IDENT, T_INT, T_KEYWORD, T_PUNCT, Token, Lexer

TYPES = {"int", "bool", "void"}

# 二元运算符 -> (优先级)
BIN_PRECEDENCE = {
    "||": 1,
    "&&": 2,
    "==": 3, "!=": 3,
    "<": 4, "<=": 4, ">": 4, ">=": 4,
    "+": 5, "-": 5,
    "*": 6, "/": 6,
}
COMPARISON_OPS = {"==", "!=", "<", "<=", ">", ">="}


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.toks = tokens
        self.pos = 0

    # ---------- token 流辅助 ----------

    def _peek(self, off: int = 0) -> Token:
        j = self.pos + off
        if j < len(self.toks):
            return self.toks[j]
        return self.toks[-1]

    def _next(self) -> Token:
        t = self.toks[self.pos]
        if t.kind != T_EOF:
            self.pos += 1
        return t

    def _at_punct(self, p: str) -> bool:
        t = self._peek()
        return t.kind == T_PUNCT and t.value == p

    def _at_kw(self, kw: str) -> bool:
        t = self._peek()
        return t.kind == T_KEYWORD and t.value == kw

    def _expect_punct(self, p: str) -> Token:
        t = self._peek()
        if not (t.kind == T_PUNCT and t.value == p):
            raise ToolError(
                PHASE_PARSER, "syntax",
                f"期望 {p!r}，但得到 {self._tok_desc(t)}",
                t.span,
            )
        return self._next()

    def _expect_kw(self, kw: str) -> Token:
        t = self._peek()
        if not (t.kind == T_KEYWORD and t.value == kw):
            raise ToolError(
                PHASE_PARSER, "syntax",
                f"期望关键字 {kw!r}，但得到 {self._tok_desc(t)}",
                t.span,
            )
        return self._next()

    @staticmethod
    def _tok_desc(t: Token) -> str:
        if t.kind == T_EOF:
            return "文件结束"
        return f"{t.value!r}"

    def _expect_ident(self) -> Token:
        t = self._peek()
        if t.kind != T_IDENT:
            raise ToolError(
                PHASE_PARSER, "syntax",
                f"期望标识符，但得到 {self._tok_desc(t)}",
                t.span,
            )
        return self._next()

    # ---------- 顶层 ----------

    def parse_program(self) -> list[ast.FuncDecl]:
        funcs: list[ast.FuncDecl] = []
        names: set[str] = set()
        while self._peek().kind != T_EOF:
            f = self._parse_func()
            if f.name in names:
                raise ToolError(
                    PHASE_PARSER, "duplicate.func",
                    f"函数 {f.name!r} 重复定义", f.span,
                )
            names.add(f.name)
            funcs.append(f)
        if not funcs:
            t = self._peek()
            raise ToolError(
                PHASE_PARSER, "empty.program", "程序至少要定义一个函数", t.span,
            )
        return funcs

    def _parse_type(self) -> str:
        t = self._peek()
        if t.kind == T_KEYWORD and t.value in TYPES:
            self._next()
            return t.value
        raise ToolError(
            PHASE_PARSER, "syntax",
            f"期望类型 (int/bool/void)，但得到 {self._tok_desc(t)}", t.span,
        )

    def _parse_func(self) -> ast.FuncDecl:
        ret_tok = self._peek()
        ret_type = self._parse_type()
        name_tok = self._expect_ident()
        self._expect_punct("(")
        params: list[tuple[str, str]] = []
        param_names: set[str] = set()
        if not self._at_punct(")"):
            while True:
                ptype = self._parse_type()
                if ptype == "void":
                    t = self._peek()
                    raise ToolError(
                        PHASE_PARSER, "syntax", "参数类型不能是 void", t.span,
                    )
                pname_tok = self._expect_ident()
                if pname_tok.value in param_names:
                    raise ToolError(
                        PHASE_PARSER, "duplicate.param",
                        f"参数 {pname_tok.value!r} 重复", pname_tok.span,
                    )
                param_names.add(pname_tok.value)
                params.append((ptype, pname_tok.value))
                if self._at_punct(","):
                    self._next()
                else:
                    break
        self._expect_punct(")")
        open_brace = self._expect_punct("{")
        body = self._parse_block_body()
        close = self._expect_punct("}")
        return ast.FuncDecl(
            span=ret_tok.span,
            name=name_tok.value,
            ret_type=ret_type,
            params=params,
            body=body,
        )

    def _parse_block_body(self) -> list:
        stmts = []
        while not self._at_punct("}") and self._peek().kind != T_EOF:
            stmts.append(self._parse_stmt())
        return stmts

    # ---------- 语句 ----------

    def _parse_stmt(self):
        t = self._peek()

        if t.kind == T_KEYWORD and t.value in ("int", "bool"):
            return self._parse_var_decl()
        if t.kind == T_KEYWORD and t.value == "if":
            return self._parse_if()
        if t.kind == T_KEYWORD and t.value == "while":
            return self._parse_while()
        if t.kind == T_KEYWORD and t.value == "return":
            return self._parse_return()

        if t.kind == T_PUNCT and t.value == ";":
            raise ToolError(
                PHASE_PARSER, "syntax", "空语句不合法", t.span,
            )

        # 赋值 或 表达式语句
        if t.kind != T_IDENT:
            raise ToolError(
                PHASE_PARSER, "syntax",
                f"非法的语句开头: {self._tok_desc(t)}", t.span,
            )
        name_tok = self._next()
        if self._at_punct("="):
            self._next()
            value = self._parse_expr()
            semi = self._expect_punct(";")
            return ast.Assign(
                span=name_tok.span, name=name_tok.value, value=value,
            )
        if self._at_punct("("):
            call = self._finish_call(name_tok)
            semi = self._expect_punct(";")
            return ast.ExprStmt(span=name_tok.span, expr=call)
        raise ToolError(
            PHASE_PARSER, "syntax",
            f"期望 '=' 或函数调用，但得到 {self._tok_desc(self._peek())}",
            self._peek().span,
        )

    def _parse_var_decl(self) -> ast.VarDecl:
        type_tok = self._next()
        name_tok = self._expect_ident()
        init = None
        if self._at_punct("="):
            self._next()
            init = self._parse_expr()
        self._expect_punct(";")
        return ast.VarDecl(
            span=type_tok.span, type=type_tok.value, name=name_tok.value, init=init,
        )

    def _parse_if(self) -> ast.IfStmt:
        kw = self._expect_kw("if")
        self._expect_punct("(")
        cond = self._parse_expr()
        self._expect_punct(")")
        self._expect_punct("{")
        then_body = self._parse_block_body()
        self._expect_punct("}")
        else_body: list = []
        if self._at_kw("else"):
            self._next()
            if self._at_kw("if"):
                else_body = [self._parse_if()]
            else:
                self._expect_punct("{")
                else_body = self._parse_block_body()
                self._expect_punct("}")
        return ast.IfStmt(span=kw.span, cond=cond, then_body=then_body,
                          else_body=else_body)

    def _parse_while(self) -> ast.WhileStmt:
        kw = self._expect_kw("while")
        self._expect_punct("(")
        cond = self._parse_expr()
        self._expect_punct(")")
        self._expect_punct("{")
        body = self._parse_block_body()
        self._expect_punct("}")
        return ast.WhileStmt(span=kw.span, cond=cond, body=body)

    def _parse_return(self) -> ast.ReturnStmt:
        kw = self._expect_kw("return")
        value = None
        if not self._at_punct(";"):
            value = self._parse_expr()
        self._expect_punct(";")
        return ast.ReturnStmt(span=kw.span, value=value)

    # ---------- 表达式（Pratt 优先级爬升） ----------

    def _parse_expr(self) -> ast.Node:
        return self._parse_binary(0)

    def _parse_binary(self, min_prec: int) -> ast.Node:
        left = self._parse_unary()
        while True:
            t = self._peek()
            if t.kind != T_PUNCT or t.value not in BIN_PRECEDENCE:
                break
            prec = BIN_PRECEDENCE[t.value]
            if prec < min_prec:
                break
            self._next()
            right = self._parse_binary(prec + 1)
            left = ast.Binary(span=left.span, op=t.value, left=left, right=right)
        return left

    def _parse_unary(self) -> ast.Node:
        t = self._peek()
        if t.kind == T_PUNCT and t.value in ("!", "-"):
            self._next()
            operand = self._parse_unary()
            return ast.Unary(span=t.span, op=t.value, operand=operand)
        return self._parse_primary()

    def _parse_primary(self) -> ast.Node:
        t = self._peek()

        if t.kind == T_INT:
            self._next()
            return ast.IntLit(span=t.span, value=t.int_value)
        if t.kind == T_KEYWORD and t.value in ("true", "false"):
            self._next()
            return ast.BoolLit(span=t.span, value=(t.value == "true"))
        if t.kind == T_PUNCT and t.value == "(":
            self._next()
            e = self._parse_expr()
            self._expect_punct(")")
            return e
        if t.kind == T_IDENT:
            self._next()
            if self._at_punct("("):
                return self._finish_call(t)
            return ast.VarRef(span=t.span, name=t.value)

        raise ToolError(
            PHASE_PARSER, "syntax",
            f"非法的表达式开头: {self._tok_desc(t)}", t.span,
        )

    def _finish_call(self, name_tok: Token) -> ast.CallExpr:
        self._expect_punct("(")
        args: list[ast.Node] = []
        if not self._at_punct(")"):
            while True:
                args.append(self._parse_expr())
                if self._at_punct(","):
                    self._next()
                else:
                    break
        self._expect_punct(")")
        return ast.CallExpr(span=name_tok.span, name=name_tok.value, args=args)


def parse_source(source: str, filename: str = "<input>") -> list[ast.FuncDecl]:
    """便捷入口：源码文本 -> AST。"""
    tokens = Lexer(source, filename).tokenize()
    return Parser(tokens).parse_program()
