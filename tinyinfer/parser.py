"""手写递归下降解析器（含 Pratt 风格的优先级爬升）。

文法（EBNF 风格，完整版见 README）::

    program := ("let" "rec"? IDENT (IDENT param)* (":" type)? "=" expr)* expr?
    expr    := seq
    seq     := assign (";" assign)*
    assign  := compare ("<-" assign)?
    compare := addinfix (("=="|"!="|"<"|"<="|">"|">=") addinfix)*
    addinfix:= mul (("+"|"-") mul)*
    mul     := unary (("*"|"/") unary)*
    unary   := ("ref"|"deref"|"not") unary | app
    app     := atom atom*
    atom    := INT | "true"|"false"|"unit" | IDENT
             | "(" expr ")" | "fun" params "->" expr
             | "let" "rec"? IDENT (params)? (":" type)? "=" expr "in" expr
             | "if" expr "then" expr "else" expr
    type    := type_atom ("->" type)?        (* 右结合 *)

顶层 ``let`` 之间、以及与收尾表达式之间以 ``let`` 关键字自然分隔，
不需要分号。
"""
from __future__ import annotations

from . import ast
from .errors import ParseError
from .lexer import Token
from .locations import Span, merge

# binary 运算符优先级表（数字越大结合越紧）
_BIN_PRECEDENCE: dict[str, int] = {
    ";": 1,
    "==": 3, "!=": 3, "<": 3, "<=": 3, ">": 3, ">=": 3,
    "+": 4, "-": 4,
    "*": 5, "/": 5,
}
_ASSIGN_PRECEDENCE = 2
# 只有 ref/deref 是语法层面的一元前缀；not 是普通的内建多态函数
# （标识符），因此 `f not` 可以把 not 本身作为值传递，`not x` 则是
# 普通函数应用，二者天然一致。
_UNARY_KW = frozenset({"ref", "deref"})
_PREFIX_KW = _UNARY_KW | frozenset({"not"})
# 顶层 let 右值的停止符：只有深度 0 的下一个顶层 "let"。
# 右值中的 fun 体 / if 分支由其自身语法（->、then、else）完整消费，
# 其中的行内 let...in 由 _parse_let 显式匹配 in，不会泄漏到这里。
_TOP_STOP = frozenset({"let"})


class Parser:
    def __init__(self, tokens: list[Token]):
        self.toks = tokens
        self.i = 0

    # ---- 停止符判定 -------------------------------------------------------
    def _is_stopped_by(self, kind: str, stop: frozenset[str]) -> bool:
        return kind in stop

    # ---- token 工具 ------------------------------------------------------
    def _peek(self, off: int = 0) -> Token | None:
        j = self.i + off
        return self.toks[j] if j < len(self.toks) else None

    def _at(self, kind: str) -> bool:
        t = self._peek()
        return t is not None and t.kind == kind

    def _advance(self) -> Token:
        t = self.toks[self.i]
        self.i += 1
        return t

    def _expect(self, kind: str, what: str | None = None) -> Token:
        t = self._peek()
        if t is None:
            raise ParseError(f"表达式意外结束，期望 {what or kind}")
        if t.kind != kind:
            raise ParseError(
                f"期望 {what or kind!r}，但遇到 {t.value!r}", t.span
            )
        return self._advance()

    # ---- 程序入口 ---------------------------------------------------------
    def parse_program(self) -> ast.Program:
        """程序语法：

        * 零或多个顶层定义 ``let [rec] f params [ann] = e``（无 in），
          它们脱糖为嵌套 let，最终体为 unit；
        * 或顶层定义之后跟一个由 ``let ... in`` 显式引出的收尾表达式
          （也可以整个程序就是一个表达式 / 一个行内 let）。

        顶层定义的右值在深度 0 的下一个顶层 "let" 处结束；带 "in" 的
        let 是表达式，由 :meth:`_parse_let` 自行消费，右值边界无歧义。
        """
        bindings: list[ast.TopLet] = []
        final_expr: ast.Expr | None = None

        while self._peek() is not None:
            if self._at("let") and not self._is_inline_let():
                bindings.append(self._parse_toplet())
            else:
                # 行内 let...in，或普通表达式：必须消耗全部剩余 token
                final_expr = self._parse_expr(frozenset())
                if self._peek() is not None:
                    t = self._peek()
                    raise ParseError(
                        f"顶层出现无法识别的 token {t.value!r}；"
                        f"顶层定义之后若要接表达式，请用 let ... in 连接",
                        t.span,
                    )

        if not bindings and final_expr is None:
            raise ParseError("空程序：至少需要一个表达式或一个 let 定义")
        return ast.Program(bindings=bindings, final_expr=final_expr)

    def _is_inline_let(self) -> bool:
        """当前 token 为 ``let`` 时，是否为消耗全部剩余 token 的行内 let。

        用于程序顶层区分"顶层定义"与"唯一收尾的行内 let 表达式"。
        解析器除游标外无副作用，可安全保存/恢复。
        """
        save_i = self.i
        try:
            self._parse_let(frozenset())
            return self._peek() is None
        except ParseError:
            return False
        finally:
            self.i = save_i

    # ---- 柯里化参数脱糖 + 标注组合（顶层 let 与行内 let 共用） ------------
    def _build_params_function(
        self,
        bound: ast.Expr,
        params: list[tuple[str, ast.Ann | None]],
        params_span: Span | None,
        result_ann: ast.Ann | None,
    ) -> tuple[ast.Expr, ast.Ann | None]:
        """把 ``f x y = e`` 形式脱糖为嵌套 Fun，并组合函数标注。"""
        if not params:
            return bound, result_ann
        body_span = bound.span
        fun_expr: ast.Expr = bound
        for pname, pann in reversed(params):
            fun_expr = ast.Fun(
                span=merge(params_span or bound.span, body_span),
                param=pname, ann=pann, body=fun_expr,
            )
        ann: ast.Ann | None = result_ann
        if result_ann is not None:
            param_anns = [pann for _n, pann in params]
            if not all(a is not None for a in param_anns):
                raise ParseError(
                    "使用结果标注 ': type' 时，所有参数都必须带标注，"
                    "如 let f (x: int): int = ...",
                    result_ann.span,
                )
            full_ann: ast.Ann = result_ann
            for pann in reversed(param_anns):
                assert pann is not None
                full_ann = ast.AnnFun(
                    span=merge(pann.span, full_ann.span),
                    param=pann, result=full_ann,
                )
            ann = full_ann
            fun_expr = _strip_fun_annotations(fun_expr)
        return fun_expr, ann

    def _parse_toplet(self) -> ast.TopLet:
        kw = self._expect("let")
        rec = False
        if self._at("rec"):
            rec = True
            self._advance()
        name_tok = self._expect("IDENT", "标识符")
        params, params_span = self._parse_params()
        ann = None
        if self._at(":"):
            self._advance()
            ann = self._parse_type()
        self._expect("=", "'='")
        # 顶层 let（无 in）：右值解析到下一个顶层 "let" 为止。
        # 右值中的 fun/if 由其语法结构完整消费，行内 let...in 自己匹配 in。
        bound = self._parse_expr(_TOP_STOP)

        span = merge(kw.span, bound.span)
        bound, ann = self._build_params_function(bound, params,
                                                  params_span, ann)
        return ast.TopLet(name=name_tok.value, bound=bound, span=span,
                          rec=rec, ann=ann)

    def _parse_params(self) -> tuple[list[tuple[str, ast.Ann | None]], Span | None]:
        """``fun``/顶层 let 的参数列表（可能为空），返回参数与整体 span。

        参数可写 ``x`` 或带括号标注 ``(x: int)``；括号外也允许
        直接标注 ``x: int``。
        """
        params: list[tuple[str, ast.Ann | None]] = []
        first_span = last_span = None

        def record(name: str, ann: ast.Ann | None, span: Span) -> None:
            nonlocal first_span, last_span
            if first_span is None:
                first_span = span
            last_span = span
            params.append((name, ann))

        while self._at("IDENT") or self._at("("):
            if self._at("("):
                # 带标注参数：(x: int)、(x: 'a -> int)
                lp = self._advance()
                tok = self._expect("IDENT", "参数名")
                pann = None
                if self._at(":"):
                    self._advance()
                    pann = self._parse_type(frozenset({")"}))
                rp = self._expect(")", "')'")
                record(tok.value, pann, merge(lp.span, rp.span))
                continue
            # 裸参数不支持内联标注（避免与返回标注的边界歧义）；
            # 请用 (x: int) 形式。
            tok = self._advance()
            if self._at(":"):
                colon = self._advance()
                raise ParseError(
                    "裸参数不能直接标注，请改用括号形式 (x: type)",
                    colon.span,
                )
            record(tok.value, None, tok.span)

        if not params:
            return [], None
        assert first_span is not None and last_span is not None
        return params, merge(first_span, last_span)

    # ---- 类型标注 ---------------------------------------------------------
    # 类型文法：
    #   type      := type_core ("->" type)?        右结合
    #   type_core := "'" IDENT | "(" type ")" | IDENT ref_suffix?
    #   ref_suffix := "ref"                          （关键字后缀）
    # 即 int / bool / unit / int ref / 'a -> 'a ref
    def _parse_type(self, stop_kinds: frozenset[str] | None = None) -> ast.Ann:
        left = self._parse_type_core(stop_kinds)
        if self._at("->"):
            self._advance()
            right = self._parse_type(stop_kinds)
            return ast.AnnFun(span=merge(left.span, right.span),
                              param=left, result=right)
        return left

    def _parse_type_core(self,
                         stop_kinds: frozenset[str] | None = None) -> ast.Ann:
        t = self._peek()
        if t is None or (stop_kinds is not None and t.kind in stop_kinds):
            raise ParseError("类型标注意外结束")
        if t.kind == "'":
            self._advance()
            name = self._expect("IDENT", "类型变量名")
            return ast.AnnVar(span=merge(t.span, name.span), name=name.value)
        if t.kind == "(":
            self._advance()
            inner = self._parse_type(stop_kinds)
            close = self._expect(")", "')'")
            result: ast.Ann = inner
            if self._at("ref"):
                kw = self._advance()
                result = ast.AnnCon(span=merge(t.span, kw.span),
                                    name="ref", args=(inner,))
            else:
                _ = close
            return result
        if t.kind == "IDENT":
            self._advance()
            if t.value not in ("int", "bool", "unit"):
                raise ParseError(
                    f"未知基础类型 {t.value!r}（可用：int/bool/unit）",
                    t.span,
                )
            base = ast.AnnCon(span=t.span, name=t.value)
            if self._at("ref"):
                kw = self._advance()
                return ast.AnnCon(span=merge(t.span, kw.span),
                                  name="ref", args=(base,))
            return base
        raise ParseError(f"期望类型，遇到 {t.value!r}", t.span)

    # ---- 表达式 -----------------------------------------------------------
    def _parse_expr(self, stop: frozenset[str]) -> ast.Expr:
        return self._parse_binop(1, stop)

    def _parse_binop(self, min_prec: int, stop: frozenset[str]) -> ast.Expr:
        left = self._parse_unary(stop)
        while True:
            t = self._peek()
            if t is None or self._is_stopped_by(t.kind, stop):
                break
            if t.kind == "<-":
                if _ASSIGN_PRECEDENCE < min_prec:
                    break
                self._advance()
                right = self._parse_assign_rhs(stop)
                left = ast.Assign(span=merge(left.span, right.span),
                                  target=left, value=right)
                continue
            prec = _BIN_PRECEDENCE.get(t.kind)
            if prec is None or prec < min_prec:
                break
            self._advance()
            right = self._parse_binop(prec + 1, stop)
            if t.kind == ";":
                left = ast.Seq(span=merge(left.span, right.span),
                               first=left, second=right)
            else:
                end = right.span.end
                left = ast.BinOp(
                    span=Span(start=left.span.start, end=end,
                              file=left.span.file),
                    op=t.kind, left=left, right=right,
                )
        return left

    def _parse_assign_rhs(self, stop: frozenset[str]) -> ast.Expr:
        # 赋值右结合：r <- x <- y  ==>  r <- (x <- y)
        return self._parse_binop(_ASSIGN_PRECEDENCE, stop)

    def _parse_unary(self, stop: frozenset[str]) -> ast.Expr:
        t = self._peek()
        if t is not None and not self._is_stopped_by(t.kind, stop) \
                and t.kind in _UNARY_KW:
            self._advance()
            arg = self._parse_unary(stop)
            if t.kind == "ref":
                return ast.Ref(span=merge(t.span, arg.span), value=arg)
            return ast.Deref(span=merge(t.span, arg.span), target=arg)
        return self._parse_app(stop)

    def _parse_app(self, stop: frozenset[str]) -> ast.Expr:
        func = self._parse_atom(stop)
        while True:
            t = self._peek()
            if t is None or self._is_stopped_by(t.kind, stop):
                break
            if t.kind in _UNARY_KW:
                # ref/deref 作为实参：解析其完整一元表达式后继续循环
                arg = self._parse_unary(stop)
                func = ast.App(span=merge(func.span, arg.span),
                               func=func, arg=arg)
                continue
            if not self._starts_atom(t):
                break
            arg = self._parse_atom(stop)
            func = ast.App(span=merge(func.span, arg.span),
                           func=func, arg=arg)
        return func

    def _starts_atom(self, t: Token | None) -> bool:
        if t is None:
            return False
        return t.kind in ("INT", "IDENT", "true", "false", "unit",
                          "(", "fun", "let", "if")

    def _parse_atom(self, stop: frozenset[str]) -> ast.Expr:
        t = self._peek()
        if t is None:
            raise ParseError("表达式意外结束")
        if t.kind in stop:
            raise ParseError(f"此处不期望出现关键字 {t.value!r}", t.span)
        kind = t.kind

        if kind == "INT":
            self._advance()
            return ast.IntLit(span=t.span, value=int(t.value))
        if kind == "true" or kind == "false":
            self._advance()
            return ast.BoolLit(span=t.span, value=(kind == "true"))
        if kind == "unit":
            self._advance()
            return ast.UnitLit(span=t.span)
        if kind == "IDENT":
            self._advance()
            return ast.Var(span=t.span, name=t.value)
        if kind == "(":
            self._advance()
            inner = self._parse_expr(stop)
            self._expect(")", "')'")
            return inner  # span 取内部即可，括号不影响错误定位
        if kind == "fun":
            return self._parse_fun(stop)
        if kind == "let":
            return self._parse_let(stop)
        if kind == "if":
            return self._parse_if(stop)
        raise ParseError(f"期望表达式，遇到 {t.value!r}", t.span)

    def _parse_fun(self, stop: frozenset[str]) -> ast.Expr:
        kw = self._expect("fun")
        params, _ = self._parse_params()
        if not params:
            bad = self._peek()
            raise ParseError("fun 后至少需要一个参数名", bad.span if bad else kw.span)
        self._expect("->", "'->'")
        body = self._parse_fun_body(stop)
        result: ast.Expr = body
        for pname, pann in reversed(params):
            result = ast.Fun(span=merge(kw.span, body.span),
                             param=pname, ann=pann, body=result)
        return result

    def _parse_fun_body(self, stop: frozenset[str]) -> ast.Expr:
        """解析函数体，自动决定顶层 "let" 是否构成函数体边界。

        在顶层定义右值上下文（``stop`` 含 "let"）中，先回溯试探：若用
        "不含 let 边界"的方式能把函数体一直合法解析到 token 末尾（或
        外层停止符），说明体内的 let 都是自洽的行内 let，应纳入函数
        体；否则 let 是下一个顶层定义的开始，函数体在其前停止。
        """
        if "let" not in stop:
            return self._parse_expr(stop)
        save_i = self.i
        try:
            extended = self._parse_expr(stop - _TOP_STOP)
            t = self._peek()
            if t is None or self._is_stopped_by(t.kind, stop - frozenset({"let"})):
                return extended  # 函数体合法地延伸过内部 let
        except ParseError:
            pass
        self.i = save_i
        return self._parse_expr(stop)

    def _parse_let(self, stop: frozenset[str]) -> ast.Expr:
        kw = self._expect("let")
        rec = False
        if self._at("rec"):
            rec = True
            self._advance()
        name_tok = self._expect("IDENT", "标识符")
        params, params_span = self._parse_params()
        ann = None
        if self._at(":"):
            self._advance()
            ann = self._parse_type()
        self._expect("=", "'='")
        bound = self._parse_expr(stop | frozenset({"in"}))
        self._expect("in", "'in'")
        body = self._parse_expr(stop)

        bound, ann = self._build_params_function(bound, params,
                                                  params_span, ann)
        return ast.Let(span=merge(kw.span, body.span), name=name_tok.value,
                       bound=bound, body=body, rec=rec, ann=ann)

    def _parse_if(self, stop: frozenset[str]) -> ast.Expr:
        kw = self._expect("if")
        cond = self._parse_expr(stop | frozenset({"then"}))
        self._expect("then", "'then'")
        then_e = self._parse_expr(stop | frozenset({"else"}))
        self._expect("else", "'else'")
        else_e = self._parse_if_branch(stop)
        return ast.If(span=merge(kw.span, else_e.span),
                      cond=cond, then=then_e, else_=else_e)

    def _parse_if_branch(self, stop: frozenset[str]) -> ast.Expr:
        """if 分支与函数体同样处理顶层 "let" 边界（回溯判定）。"""
        if "let" not in stop:
            return self._parse_expr(stop)
        save_i = self.i
        try:
            extended = self._parse_expr(stop - _TOP_STOP)
            t = self._peek()
            if t is None or self._is_stopped_by(
                t.kind, stop - frozenset({"let"})
            ):
                return extended
        except ParseError:
            pass
        self.i = save_i
        return self._parse_expr(stop)


def _strip_fun_annotations(e: ast.Expr) -> ast.Expr:
    """脱糖函数上移除参数标注（整体标注已在 let 处统一检查）。"""
    if isinstance(e, ast.Fun):
        return ast.Fun(span=e.span, param=e.param, ann=None,
                       body=_strip_fun_annotations(e.body))
    return e


def parse(tokens: list[Token]) -> ast.Program:
    return Parser(tokens).parse_program()
