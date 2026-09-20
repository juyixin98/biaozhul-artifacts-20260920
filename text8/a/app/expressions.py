"""受限条件表达式。

设计目标：条件分支只能做数据比较，**不能执行任意代码**。

做法：用 Python ``ast`` 解析为表达式后做白名单遍历，再手工求值——
不调用 ``eval``/``compile``/``getattr``，不暴露任何内建函数、导入或调用能力。

允许的语法：
- 字面量：字符串、整数、浮点数、布尔、None、list/tuple 字面量
- 变量：从流程上下文（instance.context）按名字读取，未定义即 None
- 布尔：``and`` / ``or`` / ``not``
- 比较：``== != < <= > >= in not in``
- 括号自然支持

明确禁止：函数调用、属性访问、下标、算术运算、lambda、推导式、赋值等
（任何不在白名单内的 AST 节点都抛出 RestrictedSyntaxError）。
"""
from __future__ import annotations

import ast
import operator
from typing import Any

from app.errors import ValidationError

_ALLOWED_CONSTANTS = (str, int, float, bool, type(None))

_ALLOWED_BOOL_OPS = {ast.And: operator.and_, ast.Or: operator.or_}
_ALLOWED_UNARY_OPS = {ast.Not: operator.not_}
_ALLOWED_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_MAX_EXPRESSION_NODES = 100
_MAX_NAME_LEN = 64
_MAX_CONSTANT_LEN = 500
_MAX_LIST_ITEMS = 50


class RestrictedSyntaxError(Exception):
    pass


def _validate(tree: ast.AST) -> None:
    seen = 0

    def walk(node: ast.AST) -> None:
        nonlocal seen
        seen += 1
        if seen > _MAX_EXPRESSION_NODES:
            raise RestrictedSyntaxError("表达式过于复杂")
        if isinstance(node, ast.Expression):
            walk(node.body)
        elif isinstance(node, ast.BoolOp):
            if type(node.op) not in _ALLOWED_BOOL_OPS:
                raise RestrictedSyntaxError("不支持的布尔运算")
            for v in node.values:
                walk(v)
        elif isinstance(node, ast.UnaryOp):
            if type(node.op) not in _ALLOWED_UNARY_OPS:
                raise RestrictedSyntaxError("只允许 not")
            walk(node.operand)
        elif isinstance(node, ast.Compare):
            walk(node.left)
            for op, comp in zip(node.ops, node.comparators):
                if type(op) not in _ALLOWED_CMP_OPS:
                    raise RestrictedSyntaxError("不支持的比较运算")
                walk(comp)
        elif isinstance(node, ast.Name):
            if not node.id.isidentifier() or len(node.id) > _MAX_NAME_LEN:
                raise RestrictedSyntaxError(f"非法变量名: {node.id!r}")
        elif isinstance(node, ast.Constant):
            if not isinstance(node.value, _ALLOWED_CONSTANTS):
                raise RestrictedSyntaxError("只允许标量字面量")
            if isinstance(node.value, str) and len(node.value) > _MAX_CONSTANT_LEN:
                raise RestrictedSyntaxError("字符串字面量过长")
        elif isinstance(node, (ast.List, ast.Tuple)):
            if len(node.elts) > _MAX_LIST_ITEMS:
                raise RestrictedSyntaxError("列表字面量过长")
            for elt in node.elts:
                walk(elt)
        else:
            raise RestrictedSyntaxError(
                f"不允许的语法: {type(node).__name__}"
            )

    walk(tree)


def parse_expression(text: str) -> ast.Expression:
    if not isinstance(text, str) or not text.strip():
        raise RestrictedSyntaxError("条件表达式不能为空")
    if len(text) > 1000:
        raise RestrictedSyntaxError("条件表达式过长")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise RestrictedSyntaxError(f"语法错误: {exc.msg}") from exc
    _validate(tree)
    return tree


def _eval(node: ast.AST, context: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, context)
    if isinstance(node, ast.BoolOp):
        combine = all if isinstance(node.op, ast.And) else any
        return combine(_eval(v, context) for v in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, context)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, context)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, context)
            fn = _ALLOWED_CMP_OPS[type(op)]
            try:
                ok = fn(left, right)
            except TypeError as exc:
                raise RestrictedSyntaxError(
                    f"类型不可比较: {type(left).__name__} 与 {type(right).__name__}"
                ) from exc
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        return context.get(node.id)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, context) for e in node.elts]
    # _validate 已保证不可达
    raise RestrictedSyntaxError(f"不允许的语法: {type(node).__name__}")


def safe_evaluate(expression: str, context: dict[str, Any]) -> bool:
    """求值条件表达式，返回布尔结果。供引擎条件节点调用。"""
    tree = parse_expression(expression)
    result = _eval(tree, context or {})
    return bool(result)


def validate_condition_expression(expression: str) -> None:
    """仅校验，供 DSL 发布前校验调用。"""
    try:
        parse_expression(expression)
    except RestrictedSyntaxError as exc:
        raise ValidationError(f"条件表达式非法: {exc}") from exc
