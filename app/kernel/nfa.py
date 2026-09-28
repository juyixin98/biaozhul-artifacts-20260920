"""NFA：Thompson 构造。转移携带 CharSet（区间集），接受态带规则标签。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Limits
from app.errors import ResourceExhausted
from app.kernel.charset import CharSet
from app.kernel.regex_ast import Alt, Char, Concat, Node, Repeat


@dataclass
class NFA:
    nstates: int
    start: int
    accepts: dict[int, int]  # state -> tag（规则下标）
    eps: list[list[int]] = field(default_factory=list)
    trans: list[list[tuple[CharSet, int]]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "nstates": self.nstates,
            "start": self.start,
            "accepts": {str(s): t for s, t in self.accepts.items()},
            "eps": {str(s): tgts for s, tgts in enumerate(self.eps) if tgts},
            "trans": {
                str(s): [[[lo, hi] for lo, hi in cs.intervals], t]
                for s, edges in enumerate(self.trans)
                for cs, t in edges
            },
        }


class _Builder:
    def __init__(self, limits: Limits):
        self.limits = limits
        self.eps: list[list[int]] = []
        self.trans: list[list[tuple[CharSet, int]]] = []

    def new_state(self) -> int:
        if len(self.eps) >= self.limits.max_nfa_states:
            raise ResourceExhausted(
                code="NFA_TOO_LARGE",
                message=f"NFA 状态数超过上限 {self.limits.max_nfa_states}",
                details={"limit": self.limits.max_nfa_states},
            )
        self.eps.append([])
        self.trans.append([])
        return len(self.eps) - 1

    def add_eps(self, src: int, dst: int) -> None:
        self.eps[src].append(dst)

    def add_trans(self, src: int, cs: CharSet, dst: int) -> None:
        self.trans[src].append((cs, dst))

    def build(self, node: Node) -> tuple[int, int]:
        """返回 (start, accept)。"""
        if isinstance(node, Char):
            s, a = self.new_state(), self.new_state()
            if not node.cs.is_empty():
                self.add_trans(s, node.cs, a)
            else:
                # 空字符集不可达：用死端表达（无转移）
                pass
            return s, a
        if isinstance(node, Concat):
            if not node.parts:
                s, a = self.new_state(), self.new_state()
                self.add_eps(s, a)
                return s, a
            s, acc = self.build(node.parts[0])
            for part in node.parts[1:]:
                ps, pa = self.build(part)
                self.add_eps(acc, ps)
                acc = pa
            return s, acc
        if isinstance(node, Alt):
            s, a = self.new_state(), self.new_state()
            for opt in node.options:
                os_, oa = self.build(opt)
                self.add_eps(s, os_)
                self.add_eps(oa, a)
            return s, a
        if isinstance(node, Repeat):
            return self.build_repeat(node)
        raise AssertionError(f"未知节点: {node!r}")

    def build_repeat(self, node: Repeat) -> tuple[int, int]:
        # x{m,n}：m 份必选 + (n-m) 份可选链，每个可选节点都可直接通往接受态
        s, acc = self.new_state(), self.new_state()
        head = s
        for _ in range(node.min):
            ps, pa = self.build(node.node)
            self.add_eps(head, ps)
            head = pa
        if node.max is None:
            # 尾部 x*
            ps, pa = self.build(node.node)
            self.add_eps(head, ps)
            self.add_eps(pa, head)
            self.add_eps(head, acc)
        else:
            for _ in range(node.max - node.min):
                self.add_eps(head, acc)  # 消耗 i 份后即可停止
                ps, pa = self.build(node.node)
                self.add_eps(head, ps)
                head = pa
            self.add_eps(head, acc)
        return s, acc


def epsilon_closure(nfa_eps: list[list[int]], states: set[int]) -> frozenset[int]:
    out = set(states)
    stack = list(states)
    while stack:
        s = stack.pop()
        for t in nfa_eps[s]:
            if t not in out:
                out.add(t)
                stack.append(t)
    return frozenset(out)


def ast_to_nfa(node: Node, tag: int, limits: Limits) -> NFA:
    b = _Builder(limits)
    start, accept = b.build(node)
    return NFA(
        nstates=len(b.eps),
        start=start,
        accepts={accept: tag},
        eps=b.eps,
        trans=b.trans,
    )


def combine_nfas(nfas: list[NFA], limits: Limits) -> NFA:
    """把多个单规则 NFA 合并为一个共享起始态的 NFA（标签=规则下标）。"""
    b = _Builder(limits)
    start = b.new_state()
    accepts: dict[int, int] = {}
    for nfa in nfas:
        offset = len(b.eps)
        for es in nfa.eps:
            b.eps.append([t + offset for t in es])
        for ts in nfa.trans:
            b.trans.append([(cs, t + offset) for cs, t in ts])
        b.add_eps(start, nfa.start + offset)
        for acc_state, tag in nfa.accepts.items():
            accepts[acc_state + offset] = tag
    return NFA(
        nstates=len(b.eps),
        start=start,
        accepts=accepts,
        eps=b.eps,
        trans=b.trans,
    )


def accepts_empty(nfa: NFA) -> bool:
    """语言是否包含空串：起始态的 ε 闭包是否含接受态。"""
    closure = epsilon_closure(nfa.eps, {nfa.start})
    return any(s in nfa.accepts for s in closure)
