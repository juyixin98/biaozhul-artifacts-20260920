"""简单回溯参考解释器（仅用于测试对照与灾难性回溯演示）。

**这不是**生产匹配器：它直接按 AST 递归生成所有可能的结束位置，
是朴素的回溯/分支展开语义。与 Thompson 引擎不同，它在某些模式上需要
指数级时间——这正是验收测试要对比的现象。

语义约定与 :mod:`renfa.engine` 完全一致：
  * 码点匹配、``.`` 不匹配 ``\\n``；``^`` 仅位置 0、``$`` 仅文末；
  * 搜索为左起始优先、同起始最长结束；
  * 零宽重复体可被无限量词重复任意次，但按"有限等价结果"只展开一次
    （和 Thompson NFA 的语言一致，避免无意义的无限展开）。

带递归"步数预算"：每访问一个递归节点计一步，超预算抛
:class:`BudgetExhausted`，供灾难性回溯测试观察"搜索量爆炸"。
"""

import sys

from . import ast_nodes as ast
from .lexer import tokenize
from .parser import parse

# 朴素回溯的递归深度随输入/重复次数增长，放宽解释器栈上限（步数预算仍是硬限制）。
sys.setrecursionlimit(max(sys.getrecursionlimit(), 20_000))


class BudgetExhausted(RecursionError):
    """回溯参考解释器超过步数预算——指数退化的直接信号。"""


class _Budget:
    __slots__ = ("limit", "used")

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def tick(self) -> None:
        self.used += 1
        if self.used > self.limit:
            raise BudgetExhausted(
                f"参考解释器超过 {self.limit} 步预算（回溯搜索空间爆炸）"
            )


def parse_pattern(pattern: str) -> ast.Node:
    src, toks = tokenize(pattern)
    return parse(src, toks)


def accept_positions(node: ast.Node, cps: list[int], pos: int,
                     budget: _Budget) -> "object":
    """生成器：yield 该节点从 pos 可匹配到的所有结束位置。"""
    budget.tick()
    n = len(cps)
    if isinstance(node, ast.Empty):
        yield pos
    elif isinstance(node, ast.Literal):
        if pos < n and cps[pos] == node.codepoint:
            yield pos + 1
    elif isinstance(node, ast.AnyChar):
        if pos < n and cps[pos] != 0x0A:
            yield pos + 1
    elif isinstance(node, ast.Anchor):
        if (node.kind == "^" and pos == 0) or (node.kind == "$" and pos == n):
            yield pos
    elif isinstance(node, ast.Group):
        yield from accept_positions(node.child, cps, pos, budget)
    elif isinstance(node, ast.Concat):
        yield from _seq(node.children, 0, cps, pos, budget)
    elif isinstance(node, ast.Alt):
        yield from accept_positions(node.left, cps, pos, budget)
        yield from accept_positions(node.right, cps, pos, budget)
    elif isinstance(node, ast.Repeat):
        yield from _repeat(node, cps, pos, budget)
    else:  # pragma: no cover
        raise AssertionError(f"未知节点 {type(node).__name__}")


def _seq(children: list[ast.Node], idx: int, cps: list[int], pos: int,
         budget: _Budget) -> "object":
    if idx == len(children):
        yield pos
        return
    for mid in accept_positions(children[idx], cps, pos, budget):
        yield from _seq(children, idx + 1, cps, mid, budget)


def _repeat(node: ast.Repeat, cps: list[int], pos: int,
            budget: _Budget) -> "object":
    """展开 {mn,mx}（mx=None 表示无限）。

    去重集合 seen 保证：
      1. 同一结束位置只 yield 一次（重复体可零宽时，多个重复次数会汇合）；
      2. 零宽体在无限量词下不会无限递归——某位置展开过更深一层后就不再
         沿该位置深入（语言层面等价，只影响穷举顺序，不影响结果集）。
    """
    mn, mx = node.mn, node.mx

    def go(k: int, p: int, zero_seen: set[int]) -> "object":
        budget.tick()
        # 已满足下界：当前位置是一个合法结束点。
        if k >= mn:
            yield p
        # 还能再重复一次？
        if mx is None or k < mx:
            for q in accept_positions(node.child, cps, p, budget):
                if q == p and mx is None:
                    # 无限量词 + 零宽重复体：同一位置只向更深层展开一次。
                    if p in zero_seen:
                        continue
                    zero_seen.add(p)
                yield from go(k + 1, q, zero_seen)

    seen: set[int] = set()
    for end in go(0, pos, set()):
        if end not in seen:
            seen.add(end)
            yield end


def fullmatch(pattern: str, text: str, budget: int = 2_000_000) -> bool:
    root = parse_pattern(pattern)
    cps = [ord(c) for c in text]
    b = _Budget(budget)
    return len(cps) in set(accept_positions(root, cps, 0, b))


def search(pattern: str, text: str, budget: int = 2_000_000) -> tuple[int, int] | None:
    """左起始优先、同起始最长结束。返回 (start, end) 或 None。"""
    root = parse_pattern(pattern)
    cps = [ord(c) for c in text]
    b = _Budget(budget)
    n = len(cps)
    for start in range(n + 1):
        ends = list(accept_positions(root, cps, start, b))
        if ends:
            return start, max(ends)
    return None


def steps_used_fullmatch(pattern: str, text: str, budget: int = 2_000_000) -> int:
    """跑 fullmatch 并返回消耗的步数（用于量化指数退化）。"""
    root = parse_pattern(pattern)
    cps = [ord(c) for c in text]
    b = _Budget(budget)
    set(accept_positions(root, cps, 0, b))  # 穷尽所有分支
    return b.used
