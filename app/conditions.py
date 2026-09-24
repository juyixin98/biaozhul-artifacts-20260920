"""声明式条件 AST 与 Kleene 三值逻辑解释器。

值语义
------
每个子表达式求值结果为三值之一：

* ``True``        — 可确定为真
* ``False``       — 可确定为假
* ``None``(未知)  — 引用的属性缺失，或类型不匹配导致无法判定

未知值按拒绝处理（fail-closed）：规则条件未知时规则不生效，
没有任何 allow 生效则最终决定为 DENY。

算子白名单（禁止执行任意代码——这里没有 eval/exec，也没有任何
用户可控的函数调用）：

* 逻辑：and / or / not（三值 Kleene 真值表）
* 比较：eq / neq（标量相等比较）
* 集合：in / contains / superset / subset / intersects

叶子节点
--------
* ``{"attr": "subject.department"}`` — 属性引用
* ``{"literal": <json 标量或数组>}``
* 裸标量 / 数组按字面量处理（解析期统一规范化）
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from .errors import (
    MAX_CONDITION_DEPTH,
    MAX_CONDITION_NODES,
    MAX_LITERAL_STRING_LEN,
    PolicyError,
)

# 三值
UNKNOWN = None
TRIBOOL = (True, False, None)

# 算子 -> 最少/最多参数个数
_OPERATORS: Dict[str, Tuple[int, int]] = {
    "and": (1, MAX_CONDITION_NODES),
    "or": (1, MAX_CONDITION_NODES),
    "not": (1, 1),
    "eq": (2, 2),
    "neq": (2, 2),
    "in": (2, 2),
    "contains": (2, 2),
    "superset": (2, 2),
    "subset": (2, 2),
    "intersects": (2, 2),
}

# 属性路径只允许 subject.* / resource.*，键名为安全标识符。
_PATH_RE = re.compile(r"^(subject|resource)\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_SCALAR_TYPES = (str, int, bool, float)  # JSON null 不允许作为字面量（语义模糊）


class Condition:
    """校验并规范化后的条件树。外部只通过 ``evaluate`` 使用它。"""

    def __init__(self, node: Any, *, path: str = "$", depth: int = 0) -> None:
        self.path = path
        self.depth = depth
        self.errors: List[str] = []
        self.op: str | None = None
        self.attr: str | None = None
        self.value: Any = None  # literal 的值
        self.children: List["Condition"] = []

        try:
            self._build(node, depth=depth)
        except PolicyError as exc:
            self.errors.extend(exc.details)

    # ---- 解析 / 校验 -------------------------------------------------

    def _build(self, node: Any, *, depth: int) -> None:
        if depth >= MAX_CONDITION_DEPTH:
            raise PolicyError(f"{self.path}: 条件嵌套深度超过上限 {MAX_CONDITION_DEPTH}")

        # 裸标量/数组 => 字面量
        if not isinstance(node, dict):
            self._build_literal(node)
            return

        keys = set(node.keys())

        # 属性引用
        if "attr" in keys:
            extra = keys - {"attr"}
            if extra:
                raise PolicyError(f"{self.path}: attr 节点不允许额外字段 {sorted(extra)}")
            attr = node["attr"]
            if not isinstance(attr, str):
                raise PolicyError(f"{self.path}: attr 必须是字符串")
            if not _PATH_RE.match(attr):
                raise PolicyError(
                    f"{self.path}: 非法属性路径 {attr!r}（只允许 subject.*/resource.*，"
                    "点分安全标识符）"
                )
            self.attr = attr
            return

        # 字面量
        if "literal" in keys:
            extra = keys - {"literal"}
            if extra:
                raise PolicyError(f"{self.path}: literal 节点不允许额外字段 {sorted(extra)}")
            self._build_literal(node["literal"])
            return

        # 算子节点
        op = node.get("op")
        if op is None:
            raise PolicyError(
                f"{self.path}: 节点必须是 op 算子、attr 属性引用或 literal 字面量"
            )
        if not isinstance(op, str) or op not in _OPERATORS:
            raise PolicyError(
                f"{self.path}: 未知算子 {op!r}，允许: {sorted(_OPERATORS)}"
            )
        args = node.get("args")
        if not isinstance(args, list):
            raise PolicyError(f"{self.path}.{op}: args 必须是数组")

        arity_min, arity_max = _OPERATORS[op]
        if len(args) < arity_min:
            raise PolicyError(f"{self.path}.{op}: 至少需要 {arity_min} 个参数")
        if len(args) > arity_max:
            raise PolicyError(f"{self.path}.{op}: 参数数量超过上限")

        self.op = op
        errors: List[str] = []
        for i, child in enumerate(args):
            sub = Condition(child, path=f"{self.path}.{op}[{i}]", depth=depth + 1)
            if sub.errors:
                errors.extend(sub.errors)
            self.children.append(sub)
        if errors:
            raise PolicyError(errors)

    def _build_literal(self, value: Any) -> None:
        if isinstance(value, bool) or isinstance(value, (str, int, float)):
            if isinstance(value, str) and len(value) > MAX_LITERAL_STRING_LEN:
                raise PolicyError(f"{self.path}: 字符串字面量超长（>{MAX_LITERAL_STRING_LEN}）")
            self.value = value
            return
        if isinstance(value, list):
            if not all(
                isinstance(v, (str, int, bool, float)) and not isinstance(v, type(None))
                for v in value
            ):
                raise PolicyError(f"{self.path}: 集合字面量只能包含标量")
            if any(isinstance(v, str) and len(v) > MAX_LITERAL_STRING_LEN for v in value):
                raise PolicyError(f"{self.path}: 字面量中存在超长字符串")
            self.value = value
            return
        raise PolicyError(
            f"{self.path}: 不支持的字面量类型 {type(value).__name__}"
            "（允许 bool/int/float/str 及其数组，禁止 null/对象）"
        )

    # ---- 结构统计（上限防护） ---------------------------------------

    def node_count(self) -> int:
        return 1 + sum(c.node_count() for c in self.children)

    def validate(self) -> None:
        """递归收集全部错误并做节点上限检查；有错则一次性抛出。"""
        errors: List[str] = list(self.errors)
        count = self.node_count()
        if count > MAX_CONDITION_NODES:
            errors.append(f"$: 条件节点数 {count} 超过上限 {MAX_CONDITION_NODES}")
        if errors:
            raise PolicyError(errors)

    # ---- 求值 --------------------------------------------------------

    def evaluate(self, subject: Dict[str, Any], resource: Dict[str, Any]) -> bool | None:
        """三值求值。返回 True / False / None(未知)。"""
        if self.errors:
            # 理论上 validate() 已拦截；防御性处理为未知（fail-closed）。
            return UNKNOWN

        if self.attr is not None:
            return _lookup(self.attr, subject, resource)
        if self.op is None:
            return self.value  # 字面量：标量或数组

        values = [c.evaluate(subject, resource) for c in self.children]
        return _apply_op(self.op, values)


def _lookup(path: str, subject: Dict[str, Any], resource: Dict[str, Any]) -> Any:
    """按点分路径取属性；任一键缺失或路径中段非对象 => UNKNOWN。"""
    root_name, _, rest = path.partition(".")
    root = subject if root_name == "subject" else resource
    cur: Any = root
    for part in rest.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return UNKNOWN
        cur = cur[part]
    # 属性值必须是 JSON 原生类型（防御性：None 视为缺失）
    if cur is None:
        return UNKNOWN
    if isinstance(cur, dict):
        return UNKNOWN  # 叶子必须是标量/数组；对象不可直接比较
    return cur


def _apply_op(op: str, values: List[Any]) -> bool | None:
    if op == "and":
        return _kleene_and(values)
    if op == "or":
        return _kleene_or(values)
    if op == "not":
        return _not3(values[0])

    a, b = values[0], values[1]
    if op == "eq":
        return _eq3(a, b)
    if op == "neq":
        return _not3(_eq3(a, b))
    if op in ("in", "contains", "superset", "subset", "intersects"):
        return _set_op(op, a, b)
    return UNKNOWN  # 不可达（白名单已校验）


# ---- Kleene 三值逻辑真值表 ---------------------------------------------

def _not3(v: Any) -> bool | None:
    if v is UNKNOWN:
        return UNKNOWN
    return not v


def _kleene_and(values: List[Any]) -> bool | None:
    any_unknown = False
    for v in values:
        if v is False:
            return False  # 一假即假
        if v is UNKNOWN:
            any_unknown = True
        elif v is not True:
            # 逻辑算子的子表达式必须是三值；出现其他类型 => 未知（fail-closed）
            any_unknown = True
    return UNKNOWN if any_unknown else True


def _kleene_or(values: List[Any]) -> bool | None:
    any_unknown = False
    for v in values:
        if v is True:
            return True  # 一真即真
        if v is UNKNOWN:
            any_unknown = True
        elif v is not False:
            any_unknown = True
    return UNKNOWN if any_unknown else False


# ---- 标量与集合比较（类型不符 => UNKNOWN，按拒绝处理） ------------------

def _same_scalar_type(a: Any, b: Any) -> bool:
    # bool 是 int 的子类，需显式区分
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) or \
        type(a) is type(b)


def _eq3(a: Any, b: Any) -> bool | None:
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    # eq 只比较标量
    if not isinstance(a, _SCALAR_TYPES) or not isinstance(b, _SCALAR_TYPES):
        return UNKNOWN
    if not _same_scalar_type(a, b):
        return UNKNOWN
    return a == b


def _is_set(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, _SCALAR_TYPES) for x in v)


def _set_op(op: str, a: Any, b: Any) -> bool | None:
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN

    if op == "in":
        # a ∈ b：a 标量，b 集合
        if not isinstance(a, _SCALAR_TYPES) or not _is_set(b):
            return UNKNOWN
        return _member(a, b)
    if op == "contains":
        # a ⊇ b（b 为单元素时等价于“集合 a 包含值 b”）
        if not _is_set(a):
            return UNKNOWN
        if isinstance(b, _SCALAR_TYPES):
            return _member(b, a)
        if _is_set(b):
            return all(_member(x, a) for x in b)
        return UNKNOWN
    if op == "superset":
        if not (_is_set(a) and _is_set(b)):
            return UNKNOWN
        return all(_member(x, a) for x in b)
    if op == "subset":
        if not (_is_set(a) and _is_set(b)):
            return UNKNOWN
        return all(_member(x, b) for x in a)
    if op == "intersects":
        if not (_is_set(a) and _is_set(b)):
            return UNKNOWN
        return any(_member(x, a) for x in b)
    return UNKNOWN


def _member(value: Any, collection: List[Any]) -> bool:
    for item in collection:
        eq = _eq3(value, item)
        if eq is True:
            return True
    return False
