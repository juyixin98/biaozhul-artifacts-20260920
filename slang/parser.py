"""手写递归下降语法分析器（不借助任何现成编译器/解析框架）。

运算符优先级（从低到高，同层左结合）：

    1. ||
    2. &&
    3. == !=
    4. < <= > >=
    5. + -
    6. * / %
    7. 一元 - !
    8. 基本式：字面量 / 变量 / 调用 / 括号

类型名 int/bool 是“上下文关键字”（词法上仍为 IDENT，
仅在声明位置按类型解释）。
"""

from __future__ import annotations

from . import ast_nodes as ast
from .errors import CompileError, Diagnostic
from .lexer import Token, TokenType, tokenize
from .location import SourceText

TYPE_NAMES = {ast.INT, ast.BOOL}


class Parser:
    def __init__(self, source: SourceText) -> None:
        self.src = source
        self.tokens: list[Token] = tokenize(source)
        self.i = 0

    # --- token 游标 ---
    def _peek(self, off: int = 0) -> Token:
        return self.tokens[min(self.i + off, len(self.tokens) - 1)]

    def _at(self, t: TokenType) -> bool:
        return self._peek().type is t

    def _at_ident(self, word: str) -> bool:
        t = self._peek()
        return t.type is TokenType.IDENT and t.value == word

    def _accept(self, t: TokenType) -> Token | None:
        if self._at(t):
            tok = self.tokens[self.i]
            self.i += 1
            return tok
        return None

    def _expect(self, t: TokenType, what: str) -> Token:
        if self._at(t):
            tok = self.tokens[self.i]
            self.i += 1
            return tok
        got = self._peek()
        label = got.value or got.type.name
        raise CompileError([Diagnostic(f"期望 {what}，但遇到 {label!r}", got.span)])

    def _error(self, msg: str, tok: Token | None = None) -> CompileError:
        tok = tok or self._peek()
        return CompileError([Diagnostic(msg, tok.span)])

    # ---------- 顶层 ----------
    def parse_program(self) -> ast.Program:
        start = self._peek().span
        funcs: list[ast.Func] = []
        while not self._at(TokenType.EOF):
            funcs.append(self._parse_func())
        end = self._peek().span
        return ast.Program(start.merge(end), funcs)

    def _parse_func(self) -> ast.Func:
        fn_tok = self._expect(TokenType.FN, "'fn'")
        name_tok = self._expect(TokenType.IDENT, "函数名")
        self._expect(TokenType.LPAREN, "'('")
        params: list[ast.Param] = []
        seen: set[str] = set()
        if not self._at(TokenType.RPAREN):
            while True:
                type_tok = self._expect(TokenType.IDENT, "参数类型 (int/bool)")
                if type_tok.value not in TYPE_NAMES:
                    raise self._error(f"未知类型 {type_tok.value!r}，仅支持 int/bool", type_tok)
                pname = self._expect(TokenType.IDENT, "参数名")
                if pname.value in seen:
                    raise self._error(f"参数 {pname.value!r} 重复声明", pname)
                seen.add(pname.value)
                params.append(ast.Param(type_tok.span.merge(pname.span),
                                        type_tok.value, pname.value))
                if not self._accept(TokenType.COMMA):
                    break
        self._expect(TokenType.RPAREN, "')'")
        ret_type: str | None = None
        if self._accept(TokenType.COLON):
            rt = self._expect(TokenType.IDENT, "返回类型 (int/bool)")
            if rt.value not in TYPE_NAMES:
                raise self._error(f"未知返回类型 {rt.value!r}，仅支持 int/bool", rt)
            ret_type = rt.value
        body = self._parse_block()
        return ast.Func(fn_tok.span.merge(body.span), name_tok.value, params, ret_type, body)

    # ---------- 语句 ----------
    def _parse_block(self) -> ast.Block:
        lb = self._expect(TokenType.LBRACE, "'{'")
        stmts: list[ast.Stmt] = []
        while not self._at(TokenType.RBRACE) and not self._at(TokenType.EOF):
            stmts.append(self._parse_stmt())
        rb = self._expect(TokenType.RBRACE, "'}'")
        return ast.Block(lb.span.merge(rb.span), stmts)

    def _parse_stmt(self) -> ast.Stmt:
        t = self._peek()

        if self._accept(TokenType.SEMI):
            return ast.EmptyStmt(t.span)

        if self._accept(TokenType.VAR):
            return self._parse_vardecl(t)

        # "int x = ..." / "bool x = ...;" —— 上下文关键字声明
        if t.type is TokenType.IDENT and t.value in TYPE_NAMES:
            return self._parse_typed_vardecl()

        if self._accept(TokenType.IF):
            return self._parse_if(t)
        if self._accept(TokenType.WHILE):
            return self._parse_while(t)
        if self._accept(TokenType.RETURN):
            return self._parse_return(t)
        if self._accept(TokenType.PRINT):
            return self._parse_print(t)

        # 赋值语句：IDENT "=" expr ";"  （先做 1 个 token 的前瞻判断）
        if t.type is TokenType.IDENT and self._peek(1).type is TokenType.ASSIGN:
            self.i += 1
            eq = self._accept(TokenType.ASSIGN)
            value = self._parse_expr()
            semi = self._expect(TokenType.SEMI, "';'")
            return ast.AssignStmt(t.span.merge(semi.span), t.value, value)

        # 表达式语句
        expr = self._parse_expr()
        semi = self._expect(TokenType.SEMI, "';'")
        return ast.ExprStmt(t.span.merge(semi.span), expr)

    def _parse_vardecl(self, kw: Token) -> ast.Stmt:
        # var x = expr;  —— 不带类型，类型由初始化式推断
        name = self._expect(TokenType.IDENT, "变量名")
        self._expect(TokenType.ASSIGN, "'='")
        init = self._parse_expr()
        semi = self._expect(TokenType.SEMI, "';'")
        vd = ast.VarDecl(kw.span.merge(semi.span), "", name.value, init)
        vd.inferred = True
        return vd

    def _parse_typed_vardecl(self) -> ast.Stmt:
        # int x; | int x = expr; | bool b; | bool b = expr;
        type_tok = self.tokens[self.i]
        self.i += 1
        name = self._expect(TokenType.IDENT, "变量名")
        init: ast.Expr | None = None
        if self._accept(TokenType.ASSIGN):
            init = self._parse_expr()
        semi = self._expect(TokenType.SEMI, "';'")
        return ast.VarDecl(type_tok.span.merge(semi.span), type_tok.value, name.value, init)

    def _parse_if(self, kw: Token) -> ast.Stmt:
        self._expect(TokenType.LPAREN, "'('")
        cond = self._parse_expr()
        self._expect(TokenType.RPAREN, "')'")
        then_blk = self._parse_block()
        else_blk = None
        if self._accept(TokenType.ELSE):
            else_blk = self._parse_block()
        return ast.IfStmt(kw.span.merge((else_blk or then_blk).span),
                          cond, then_blk, else_blk)

    def _parse_while(self, kw: Token) -> ast.Stmt:
        self._expect(TokenType.LPAREN, "'('")
        cond = self._parse_expr()
        self._expect(TokenType.RPAREN, "')'")
        body = self._parse_block()
        return ast.WhileStmt(kw.span.merge(body.span), cond, body)

    def _parse_return(self, kw: Token) -> ast.Stmt:
        value: ast.Expr | None = None
        if not self._at(TokenType.SEMI):
            value = self._parse_expr()
        semi = self._expect(TokenType.SEMI, "';'")
        return ast.ReturnStmt(kw.span.merge(semi.span), value)

    def _parse_print(self, kw: Token) -> ast.Stmt:
        value = self._parse_expr()
        semi = self._expect(TokenType.SEMI, "';'")
        return ast.PrintStmt(kw.span.merge(semi.span), value)

    # ---------- 表达式（优先级分层） ----------
    _BINARY_TIERS = (
        ((TokenType.OR, "||"),),
        ((TokenType.AND, "&&"),),
        ((TokenType.EQ, "=="), (TokenType.NE, "!=")),
        ((TokenType.LT, "<"), (TokenType.LE, "<="),
         (TokenType.GT, ">"), (TokenType.GE, ">=")),
        ((TokenType.PLUS, "+"), (TokenType.MINUS, "-")),
        ((TokenType.STAR, "*"), (TokenType.SLASH, "/"), (TokenType.PERCENT, "%")),
    )

    def _parse_expr(self) -> ast.Expr:
        return self._parse_binary(0)

    def _parse_binary(self, tier: int) -> ast.Expr:
        if tier >= len(self._BINARY_TIERS):
            return self._parse_unary()
        left = self._parse_binary(tier + 1)
        ops = self._BINARY_TIERS[tier]
        while True:
            matched: tuple[TokenType, str] | None = None
            for tt, sym in ops:
                if self._at(tt):
                    matched = (tt, sym)
                    break
            if matched is None:
                return left
            op_tok = self.tokens[self.i]
            self.i += 1
            right = self._parse_binary(tier + 1)
            left = ast.Binary(op_tok.span.merge(right.span), matched[1], left, right)

    def _parse_unary(self) -> ast.Expr:
        if self._at(TokenType.MINUS) or self._at(TokenType.BANG):
            op_tok = self.tokens[self.i]
            self.i += 1
            operand = self._parse_unary()
            sym = "-" if op_tok.type is TokenType.MINUS else "!"
            return ast.Unary(op_tok.span.merge(operand.span), sym, operand)
        return self._parse_primary()

    def _parse_primary(self) -> ast.Expr:
        t = self._peek()

        if self._accept(TokenType.INT):
            try:
                val = int(t.value)
            except ValueError:
                raise self._error(f"整数字面量超出范围: {t.value}", t)
            return ast.IntLit(t.span, val)

        if self._accept(TokenType.TRUE):
            return ast.BoolLit(t.span, True)
        if self._accept(TokenType.FALSE):
            return ast.BoolLit(t.span, False)

        if self._accept(TokenType.LPAREN):
            expr = self._parse_expr()
            self._expect(TokenType.RPAREN, "')'")
            return expr

        if self._accept(TokenType.IDENT):
            if self._accept(TokenType.LPAREN):
                args: list[ast.Expr] = []
                if not self._at(TokenType.RPAREN):
                    while True:
                        args.append(self._parse_expr())
                        if not self._accept(TokenType.COMMA):
                            break
                rp = self._expect(TokenType.RPAREN, "')'")
                return ast.Call(t.span.merge(rp.span), t.value, args)
            return ast.VarRef(t.span, t.value)

        label = t.value or t.type.name
        raise self._error(f"期望表达式，但遇到 {label!r}", t)


def parse_source(source: SourceText) -> ast.Program:
    return Parser(source).parse_program()
