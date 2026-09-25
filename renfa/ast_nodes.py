"""抽象语法树节点定义。

每个节点都携带源码 ``span``（半开码点区间），实现"保留源码位置"的要求：
报错、调试输出与 JSON 服务中的 ``ast`` 都能回溯到模式中的行列。
"""

from dataclasses import dataclass, field

from .source import Span


class Node:
    span: Span

    def describe(self) -> str:  # pragma: no cover - 仅用于报错信息
        raise NotImplementedError


@dataclass
class Empty(Node):
    span: Span


@dataclass
class Literal(Node):
    codepoint: int
    span: Span


@dataclass
class AnyChar(Node):
    """. —— 匹配除 \\n 以外的任意单个码点。"""

    span: Span


@dataclass
class Anchor(Node):
    """零宽锚点；kind 为 '^' 或 '$'。"""

    kind: str
    span: Span


@dataclass
class Concat(Node):
    children: list[Node]
    span: Span


@dataclass
class Alt(Node):
    left: Node
    right: Node
    span: Span


@dataclass
class Group(Node):
    child: Node
    span: Span


@dataclass
class Repeat(Node):
    """有限重复（兼容无限上界）。

    mn/mx 来自 {n}、{n,}、{n,m}；*、+、? 在 parser 中归一化为本节点：
        *  -> (0, None)，+ -> (1, None)，? -> (0, 1)
    本引擎不支持反向引用，也不做捕获，Group 只起优先级作用。
    """

    child: Node
    mn: int
    mx: int | None
    span: Span
