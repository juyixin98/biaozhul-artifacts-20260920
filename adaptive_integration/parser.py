"""安全的数学表达式解析器（白名单 AST，禁止任意代码执行）。

JSON 接口中的被积函数以字符串给出，例如 ``"exp(-x)*sin(10*x)"``。
为避免 ``eval`` 注入，这里：

1. 用 :func:`ast.parse` 解析表达式（仅允许 ``eval`` 模式）；
2. 递归校验语法树，只放行数值字面量、变量 ``x``、白名单二元/一元运算
   与白名单数学函数；禁止属性访问、名称（除白名单常量外）、调用非白名单
   函数、关键字参数、比较运算、布尔运算等；
3. 校验通过后把表达式编译成 code object，在 ``{"__builtins__": {}}`` 的
   命名空间中执行。

同时限制表达式长度（200 字符）、AST 深度（30）与字面量绝对值（1e6），
避免字面量级 DoS（如 ``9**9**9**9``）。
"""

from __future__ import annotations

import ast
import math
from typing import Any

from .core import MAX_EXPRESSION_CHARS, MAX_LITERAL_MAG

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)

_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}

# 1 参函数白名单
_FUNCTIONS_1 = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "asinh": math.asinh, "acosh": math.acosh, "atanh": math.atanh,
    "exp": math.exp, "log": math.log, "log10": math.log10,
    "log2": math.log2, "sqrt": math.sqrt, "cbrt": lambda y: math.copysign(abs(y) ** (1.0 / 3.0), y),
    "fabs": math.fabs, "floor": math.floor, "ceil": math.ceil,
    "erf": math.erf, "erfc": math.erfc, "gamma": math.gamma,
    "lgamma": math.lgamma,
    "arcsin": math.asin, "arccos": math.acos, "arctan": math.atan,
    "ln": math.log,
}
# 2 参函数白名单
_FUNCTIONS_2 = {
    "pow": math.pow, "atan2": math.atan2,
    "max": max, "min": min, "fmod": math.fmod,
}

_MAX_AST_DEPTH = 30


class ParseError(ValueError):
    """表达式不合法或使用了白名单之外的语法。"""


def _check_no_pow_chain(node: ast.AST, depth: int) -> None:
    if depth > 3:
        raise ParseError("幂运算连续嵌套过深（最多 3 层）。")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        # ** 右结合，链可能在任一侧，两侧都检查
        _check_no_pow_chain(node.left, depth + 1)
        _check_no_pow_chain(node.right, depth + 1)


class _FloatLiterals(ast.NodeTransformer):
    """把全部数值字面量转成 float，杜绝大整数 DoS（如 9**9**9**9）。

    转成浮点后，超大幂运算立刻抛 OverflowError 而不是消耗海量内存。
    """

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ParseError("只允许数值常量（不允许字符串/复数/布尔值）。")
        value = float(node.value)
        if not math.isfinite(value):
            raise ParseError("不允许 NaN/Infinity 常量。")
        if abs(value) > MAX_LITERAL_MAG:
            raise ParseError(f"常量绝对值不得超过 {MAX_LITERAL_MAG:g}。")
        return ast.copy_location(ast.Constant(value=value), node)


def _check_depth(node: ast.AST, depth: int = 0) -> None:
    if depth > _MAX_AST_DEPTH:
        raise ParseError(f"表达式嵌套过深（超过 {_MAX_AST_DEPTH} 层）。")
    for child in ast.iter_child_nodes(node):
        _check_depth(child, depth + 1)


def _validate(node: ast.AST) -> None:
    if isinstance(node, ast.Expression):
        _validate(node.body)
    elif isinstance(node, ast.Constant):
        # _FloatLiterals 已保证这里是有限、幅值受限的 float
        if not isinstance(node.value, float):
            raise ParseError("只允许数值常量。")
    elif isinstance(node, ast.Name):
        if node.id != "x" and node.id not in _CONSTANTS:
            raise ParseError(
                f"不允许的名称 {node.id!r}；唯一变量是 x，"
                f"可用常量为 {sorted(_CONSTANTS)}。")
    elif isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise ParseError(f"不允许的二元运算符: {type(node.op).__name__}")
        if isinstance(node.op, ast.Pow):
            # 限制幂运算连续嵌套（浮点下虽不挂起，但仍属明显 DoS 形态）
            _check_no_pow_chain(node, 0)
        _validate(node.left)
        _validate(node.right)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARYOPS):
            raise ParseError(f"不允许的一元运算符: {type(node.op).__name__}")
        _validate(node.operand)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ParseError("只允许调用白名单中的数学函数，禁止方法/属性调用。")
        name = node.func.id
        if name not in _FUNCTIONS_1 and name not in _FUNCTIONS_2:
            raise ParseError(f"函数 {name!r} 不在白名单中。")
        if node.keywords:
            raise ParseError("不允许关键字参数。")
        arity = 1 if name in _FUNCTIONS_1 else 2
        if len(node.args) != arity:
            raise ParseError(f"函数 {name} 需要 {arity} 个参数，收到 {len(node.args)} 个。")
        for arg in node.args:
            _validate(arg)
    else:
        raise ParseError(f"不允许的语法元素: {type(node).__name__}")


class ParsedFunction:
    """编译后的可求值表达式。求值异常被转换为 :class:`EvalError`。"""

    def __init__(self, expression: str):
        if not isinstance(expression, str):
            raise ParseError("expression 必须是字符串。")
        if len(expression) > MAX_EXPRESSION_CHARS:
            raise ParseError(
                f"表达式长度不得超过 {MAX_EXPRESSION_CHARS} 个字符。")
        if not expression.strip():
            raise ParseError("表达式为空。")
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ParseError(f"表达式语法错误: {exc.msg}") from exc
        _check_depth(tree)
        tree = _FloatLiterals().visit(tree)
        ast.fix_missing_locations(tree)
        _validate(tree)
        self._code = compile(tree, "<integrand>", "eval")
        self._namespace = {"__builtins__": {}}
        self._namespace.update(_CONSTANTS)
        self._namespace.update(_FUNCTIONS_1)
        self._namespace.update(_FUNCTIONS_2)
        self.expression = expression

    def __call__(self, x: float) -> float:
        # 实数域上的“不可求值”（除零、log 负数、负底偶次根、exp 溢出）
        # 统一映射为 inf/nan：核心层的非有限值检查会据此判出端点/内部奇点
        # 并给出位置明确的失败信息。
        try:
            y = eval(self._code, self._namespace, {"x": float(x)})  # noqa: S307
        except ZeroDivisionError:
            return float("inf")
        except (ValueError, OverflowError):
            return float("nan")
        y = float(y)
        if not math.isfinite(y):
            # inf/nan 直接返回，由 core 的求值包装识别并分类
            return y
        return y


class EvalError(Exception):
    """保留用于外部扩展；默认求值路径把数学错误映射为 inf/nan。"""
