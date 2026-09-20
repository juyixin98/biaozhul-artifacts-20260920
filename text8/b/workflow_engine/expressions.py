"""受限表达式求值器。

设计原则：
- 只做 **解析期白名单 + 递归求值**，从不调用 ``eval`` / ``exec`` / ``compile`` 执行。
- 允许：字面量、上下文中的变量名、算术/比较/布尔运算、三元表达式、in/not in、
  list/tuple 字面量。
- 禁止：函数调用、属性访问、下标、推导式、lambda、f-string、星号解包、海象运算符；
  也禁止 bytes/省略号等非常量字面量。
- 变量只能取自实例上下文（白名单 dict），不暴露任何 Python 内建或环境。

典型表达式：
    amount > 10000 and level == "senior"
    department in ["finance", "legal"]
    risk_score >= 80 or vip is True
"""

import ast
import operator

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_ALLOWED_BOOLOPS = {ast.And: all, ast.Or: any}

_ALLOWED_UNARYOPS = {
    ast.Not: operator.not_,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_ALLOWED_CMPOPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: lambda a, b: a is b,
    ast.IsNot: lambda a, b: a is not b,
}

_ALLOWED_CONST_TYPES = (bool, int, float, str, type(None))

# 幂运算的指数上界，防止 2**99999... 制造大整数 DoS
_MAX_POW_EXP = 64


class ExpressionError(ValueError):
    """表达式不合法或求值失败。"""


def parse_expression(expr: str) -> ast.Expression:
    if not isinstance(expr, str) or not expr.strip():
        raise ExpressionError("表达式为空")
    if len(expr) > 1000:
        raise ExpressionError("表达式过长（最多 1000 字符）")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"语法错误: {exc.msg}") from exc
    _validate(tree)
    return tree


def _validate(tree: ast.AST) -> None:
    # 运算符与变量上下文是“哨兵节点”，由其父节点负责检查
    operator_sentinels = (
        ast.operator,
        ast.boolop,
        ast.unaryop,
        ast.cmpop,
        ast.expr_context,
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Expression) or isinstance(node, operator_sentinels):
            continue
        if isinstance(node, ast.BoolOp) and type(node.op) in _ALLOWED_BOOLOPS:
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            continue
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
            continue
        if isinstance(node, ast.Compare):
            if not node.ops or any(type(op) not in _ALLOWED_CMPOPS for op in node.ops):
                raise ExpressionError("只允许 ==, !=, <, <=, >, >=, in, not in, is, is not 比较")
            continue
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, _ALLOWED_CONST_TYPES):
                raise ExpressionError(f"不支持的字面量类型: {type(node.value).__name__}")
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id.startswith("__"):
                raise ExpressionError(f"非法变量名: {node.id}")
            continue
        if isinstance(node, (ast.List, ast.Tuple)):
            continue
        if isinstance(node, ast.IfExp):
            continue
        raise ExpressionError(f"不允许的表达式元素: {type(node).__name__}")


def safe_eval(expr: str, context: dict) -> bool:
    """在给定上下文中求表达式布尔值。"""
    tree = parse_expression(expr)
    value = _evaluate(tree.body, context or {})
    return bool(value)


def _evaluate(node: ast.AST, context: dict):
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, context)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in context:
            raise ExpressionError(f"未知变量: {node.id}")
        return context[node.id]
    if isinstance(node, ast.BoolOp):
        reducer = _ALLOWED_BOOLOPS[type(node.op)]
        return reducer(_evaluate(v, context) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        return _ALLOWED_UNARYOPS[type(node.op)](_evaluate(node.operand, context))
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_BINOPS[type(node.op)]
        left = _evaluate(node.left, context)
        right = _evaluate(node.right, context)
        if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and abs(right) > _MAX_POW_EXP:
            raise ExpressionError("幂运算指数过大")
        try:
            return op(left, right)
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise ExpressionError(f"运算失败: {exc}") from exc
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, context)
        for op_node, comparator in zip(node.ops, node.comparators, strict=True):
            right = _evaluate(comparator, context)
            try:
                if not _ALLOWED_CMPOPS[type(op_node)](left, right):
                    return False
            except TypeError as exc:
                raise ExpressionError(f"比较类型不匹配: {exc}") from exc
            left = right
        return True
    if isinstance(node, ast.IfExp):
        branch = node.body if _evaluate(node.test, context) else node.orelse
        return _evaluate(branch, context)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_evaluate(e, context) for e in node.elts]
    raise ExpressionError(f"不允许的表达式元素: {type(node).__name__}")
