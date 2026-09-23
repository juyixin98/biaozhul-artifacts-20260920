"""语法树节点与解析诊断。

位置约定
========
* 字符区间一律为半开区间 ``[start, end)``，与 Python 切片一致；
* EOF 哨兵位置取源码长度，其令牌区间为 ``[n, n)``（n = 有效令牌数）；
* 每个节点同时记录字符区间（``start``/``end``）与令牌区间
  （``tok_start``/``tok_end``），增量解析据此重定位复用节点。

节点的 ``children`` 是规范化后的有序列表；同一种节点类型，
子节点的顺序与含义固定（见 README“节点格式”一节）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Node:
    """不可变语义的语法树节点（增量解析时整棵复用或整体替换）。"""

    kind: str
    start: int
    end: int
    tok_start: int
    tok_end: int
    # 语义负载：标识符名、运算符、字面量原文等；无负载时为 None
    text: str | None = None
    children: list["Node"] = field(default_factory=list)
    # 该节点自身解析例程产生的错误（不含后代产生的错误）。
    # 增量复用子树时，连同后代的 owned 错误一起克隆并平移位置。
    owned_errors: list["ParseError"] = field(default_factory=list)
    # 解析期单调编号；复用节点保留旧编号，新节点取更大的编号。
    node_id: int = 0
    # 该节点由哪个解析例程产生（解析表主键），仅增量解析内部使用，
    # 不参与规范化树比较，也不写入对外 JSON。
    pkey: str | None = None
    # 仅零宽度包裹节点（空 ParamList/ArgList）使用：位置所锚定的
    # 前一个令牌下标（通常是开括号），供增量克隆重定位。
    tok_anchor: int | None = None

    # 叶子形态（无子节点）的 text 含义：
    #   Number   -> 数字原文        String    -> 字符串原文（不含引号）
    #   Ident    -> 标识符名        Unary     -> 运算符（- !）
    #   Binary   -> 运算符          Break/Continue/Empty -> None


@dataclass(slots=True)
class ParseError:
    """语法诊断。``start`` 处为半开锚点区间，EOF 错误锚点为 [len, len)。"""

    message: str
    start: int
    end: int

    def shifted(self, delta: int) -> "ParseError":
        return ParseError(self.message, self.start + delta, self.end + delta)

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "start": self.start, "end": self.end}
