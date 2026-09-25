"""安全的数学表达式解析器（不使用 eval/exec/compile）。

实现一个小型递归下降 + 运算符优先级解析器，仅支持数值、变量 x、常用
一元函数与二元算术运算，用于从 JSON 请求中把被积函数编译成可调用对象。

语法（按优先级从低到高）：
    expr       := term (('+' | '-') term)*
    term       := factor (('*' | '/') factor)*
    factor     := unary ('^' factor)?        # 右结合
    unary      := ('+' | '-') unary | atom
    atom       := NUMBER | 'x' | NAME '(' expr (',' expr)* ')' | '(' expr ')'

不支持隐式乘法（如 2x、2(x+1)），以保持错误信息明确。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, List

import numpy as np


class ParseError(ValueError):
    """表达式词法/语法/未知符号错误。"""


# ---------------------------------------------------------------------------
# 词法分析
# ---------------------------------------------------------------------------

@dataclass
class Token:
    kind: str          # 'num' | 'name' | 'var' | 'op' | '(' | ')' | ','
    value: object
    pos: int


def _tokenize(text: str) -> List[Token]:
    tokens: List[Token] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c.isdigit() or (c == "."):
            j = i
            seen_dot = False
            seen_e = False
            while j < n:
                d = text[j]
                if d.isdigit():
                    j += 1
                elif d == "." and not seen_dot and not seen_e:
                    seen_dot = True
                    j += 1
                elif d in "eE" and not seen_e and j > i:
                    # 1.5e-3、2e+2
                    seen_e = True
                    j += 1
                    if j < n and text[j] in "+-":
                        j += 1
                    if j >= n or not text[j].isdigit():
                        raise ParseError(
                            f"位置 {j}: 科学计数法缺少指数数字"
                        )
                else:
                    break
            word = text[i:j]
            if word in (".", ""):
                raise ParseError(f"位置 {i}: 非法数字")
            tokens.append(Token("num", float(word), i))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            name = text[i:j]
            if name == "x":
                tokens.append(Token("var", "x", i))
            else:
                tokens.append(Token("name", name, i))
            i = j
            continue
        if c in "+-*/^":
            tokens.append(Token("op", c, i))
            i += 1
            continue
        if c == "(":
            tokens.append(Token("(", c, i))
            i += 1
            continue
        if c == ")":
            tokens.append(Token(")", c, i))
            i += 1
            continue
        if c == ",":
            tokens.append(Token(",", c, i))
            i += 1
            continue
        raise ParseError(f"位置 {i}: 无法识别的字符 {c!r}")
    return tokens


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

class Node:
    def eval(self, x):  # pragma: no cover - 由子类实现
        raise NotImplementedError


class _Num(Node):
    __slots__ = ("v",)

    def __init__(self, v: float):
        self.v = v

    def eval(self, x):
        return self.v


class _Var(Node):
    __slots__ = ()

    def eval(self, x):
        return x


class _Bin(Node):
    __slots__ = ("op", "a", "b")

    def __init__(self, op: str, a: Node, b: Node):
        self.op = op
        self.a = a
        self.b = b

    def eval(self, x):
        a = self.a.eval(x)
        b = self.b.eval(x)
        op = self.op
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                return a * b
        if op == "/":
            with np.errstate(divide="ignore", invalid="ignore"):
                # np.true_divide 保证标量 1/0 也返回 inf 而非抛异常
                return np.true_divide(a, b)
        # '^'
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.power(a, b)


class _Neg(Node):
    __slots__ = ("child",)

    def __init__(self, child: Node):
        self.child = child

    def eval(self, x):
        return -self.child.eval(x)


class _Call(Node):
    __slots__ = ("fname", "args", "fn", "nargs")

    def __init__(self, fname: str, args: List[Node], fn, nargs):
        self.fname = fname
        self.args = args
        self.fn = fn
        self.nargs = nargs

    def eval(self, x):
        vals = [a.eval(x) for a in self.args]
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            return self.fn(*vals)


# ---------------------------------------------------------------------------
# 允许的符号白名单
# ---------------------------------------------------------------------------

def _const_pi():
    return math.pi


def _const_e():
    return math.e


def _arctan2(y, x):
    return np.arctan2(y, x)


FUNCTIONS: dict[str, tuple[Callable, int]] = {
    # 名称: (向量化函数, 参数个数)
    "abs": (np.abs, 1),
    "sqrt": (np.sqrt, 1),
    "exp": (np.exp, 1),
    "log": (np.log, 1),
    "ln": (np.log, 1),
    "log2": (np.log2, 1),
    "log10": (np.log10, 1),
    "sin": (np.sin, 1),
    "cos": (np.cos, 1),
    "tan": (np.tan, 1),
    "asin": (np.arcsin, 1),
    "acos": (np.arccos, 1),
    "atan": (np.arctan, 1),
    "sinh": (np.sinh, 1),
    "cosh": (np.cosh, 1),
    "tanh": (np.tanh, 1),
    "atan2": (_arctan2, 2),
}

CONSTANTS: dict[str, Callable[[], float]] = {
    "pi": _const_pi,
    "PI": _const_pi,
    "e": _const_e,
    "E": _const_e,
}


# ---------------------------------------------------------------------------
# 递归下降解析器
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.i = 0

    def _peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _take(self):
        tok = self._peek()
        self.i += 1
        return tok

    def parse(self) -> Node:
        if not self.tokens:
            raise ParseError("表达式为空")
        node = self._expr()
        if self._peek() is not None:
            tok = self._peek()
            raise ParseError(f"位置 {tok.pos}: 意外的符号 {tok.value!r}")
        return node

    def _expr(self) -> Node:
        node = self._term()
        while True:
            tok = self._peek()
            if tok is not None and tok.kind == "op" and tok.value in "+-":
                self._take()
                rhs = self._term()
                node = _Bin(tok.value, node, rhs)
            else:
                return node

    def _term(self) -> Node:
        node = self._factor()
        while True:
            tok = self._peek()
            if tok is not None and tok.kind == "op" and tok.value in "*/":
                self._take()
                rhs = self._factor()
                node = _Bin(tok.value, node, rhs)
            else:
                return node

    def _factor(self) -> Node:
        # 一元负号优先级低于幂运算：-x^2 == -(x^2)；幂运算右结合。
        tok = self._peek()
        if tok is not None and tok.kind == "op" and tok.value in "+-":
            self._take()
            child = self._factor()
            return child if tok.value == "+" else _Neg(child)
        base = self._atom()
        tok = self._peek()
        if tok is not None and tok.kind == "op" and tok.value == "^":
            self._take()
            exponent = self._factor()  # 右结合
            return _Bin("^", base, exponent)
        return base

    def _unary(self) -> Node:
        # 兼容内部调用：幂运算层已包含一元正负号处理
        return self._factor()

    def _atom(self) -> Node:
        tok = self._take()
        if tok is None:
            raise ParseError("表达式不完整：意外结束")
        if tok.kind == "num":
            return _Num(tok.value)
        if tok.kind == "var":
            return _Var()
        if tok.kind == "name":
            name = tok.value
            # 常量（不允许带括号调用形式）
            if name in CONSTANTS:
                return _Num(CONSTANTS[name]())
            if name not in FUNCTIONS:
                raise ParseError(
                    f"位置 {tok.pos}: 未知函数或常量 {name!r}"
                )
            fn, nargs = FUNCTIONS[name]
            open_paren = self._take()
            if open_paren is None or open_paren.kind != "(":
                raise ParseError(
                    f"位置 {tok.pos}: 函数 {name} 后缺少 '('"
                )
            args: List[Node] = [self._expr()]
            while True:
                comma = self._peek()
                if comma is None:
                    raise ParseError(f"函数 {name} 的参数列表未闭合")
                if comma.kind == ")":
                    self._take()
                    break
                if comma.kind == ",":
                    self._take()
                    args.append(self._expr())
                else:
                    raise ParseError(
                        f"位置 {comma.pos}: 函数参数中应为 ',' 或 ')'"
                    )
            if len(args) != nargs:
                raise ParseError(
                    f"函数 {name} 需要 {nargs} 个参数，得到 {len(args)} 个"
                )
            return _Call(name, args, fn, nargs)
        if tok.kind == "(":
            node = self._expr()
            close = self._take()
            if close is None or close.kind != ")":
                raise ParseError(f"位置 {tok.pos}: 缺少匹配的 ')'")
            return node
        raise ParseError(f"位置 {tok.pos}: 意外的符号 {tok.value!r}")


# ---------------------------------------------------------------------------
# 编译入口
# ---------------------------------------------------------------------------

def compile_expression(text: str) -> Callable[[object], object]:
    """把字符串编译成 f(x)。

    返回的函数同时支持标量与 NumPy 数组输入；内部不做合法性过滤，
    非有限值由积分驱动层统一检测。
    """

    if not isinstance(text, str):
        raise ParseError("表达式必须是字符串")
    if len(text) > 500:
        raise ParseError("表达式过长（上限 500 个字符）")
    tokens = _tokenize(text)
    ast = _Parser(tokens).parse()

    def f(x):
        return ast.eval(x)

    f.__doc__ = f"compiled expression: {text}"
    return f
