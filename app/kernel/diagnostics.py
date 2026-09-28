"""重叠诊断：两规则交集的最短见证、不可达规则。

- 最短见证：两 NFA 的积自动机上 0-1 BFS（ε 边权 0，字符边权 1），
  首次弹出的双接受态即最短见证；字符取交集区间最小码点，保证确定性。
- 不可达规则：规则 R 不可达 当且仅当 L(R) ⊆ ⋃ L(R')，其中 R' 遍历
  所有能在等长时击败 R 的规则（优先级更高，或同优先级但声明在前）。
  判定：L(R) ∩ complement(⋃ L(R')) 是否为空（补自动机需先完备化）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from app.config import Limits
from app.errors import ResourceExhausted
from app.kernel.charset import CharSet
from app.kernel.dfa import complement_dfa, complete_dfa, nfa_to_dfa
from app.kernel.lexer import Rule
from app.kernel.nfa import NFA, combine_nfas
from app.runlog import RunLogger

MODULE = "kernel.diagnostics"
UNIVERSAL_TAG = -2


def universal_nfa() -> NFA:
    """接受全字母表任意串的单态 NFA（含空串），用于单语言最短路。"""
    return NFA(
        nstates=1,
        start=0,
        accepts={0: UNIVERSAL_TAG},
        eps=[[]],
        trans=[[(CharSet.full(), 0)]],
    )


def shortest_shared_string(a: NFA, b: NFA, limits: Limits) -> str | None:
    """L(a) ∩ L(b) 中的最短串；无交集返回 None。结果确定性。"""
    start = (a.start, b.start)
    dist = {start: 0}
    parent: dict[tuple[int, int], tuple[tuple[int, int], str | None]] = {}
    done: set[tuple[int, int]] = set()
    dq: deque[tuple[int, int]] = deque([start])
    while dq:
        sa, sb = dq.popleft()
        if (sa, sb) in done:
            continue  # 过期队列项：该状态已以最短距离定型
        done.add((sa, sb))
        d = dist[(sa, sb)]
        if sa in a.accepts and sb in b.accepts:
            # 0-1 BFS 按距离非递减定型，首个双接受态即最短
            chars: list[str] = []
            cur = (sa, sb)
            while cur in parent:
                prev, ch = parent[cur]
                if ch is not None:
                    chars.append(ch)
                cur = prev
            return "".join(reversed(chars))

        def relax(nxt: tuple[int, int], nd: int, ch: str | None) -> None:
            if nxt in done or (nxt in dist and dist[nxt] <= nd):
                return
            dist[nxt] = nd
            parent[nxt] = ((sa, sb), ch)
            if nd == d:
                dq.appendleft(nxt)
            else:
                dq.append(nxt)

        # ε 边（权 0）
        for ta in a.eps[sa]:
            relax((ta, sb), d, None)
        for tb in b.eps[sb]:
            relax((sa, tb), d, None)
        # 字符边（权 1）：两自动机转移字符集相交
        for csa, ta in a.trans[sa]:
            for csb, tb in b.trans[sb]:
                inter = csa.intersect(csb)
                if inter.is_empty():
                    continue
                relax((ta, tb), d + 1, chr(inter.min_char()))
        if len(dist) > limits.max_product_states:
            raise ResourceExhausted(
                code="PRODUCT_TOO_LARGE",
                message=f"积自动机状态数超过上限 {limits.max_product_states}",
                details={"limit": limits.max_product_states},
            )
    return None


@dataclass(frozen=True)
class OverlapEntry:
    rule_a: str
    rule_b: str
    witness: str

    def to_dict(self) -> dict:
        return {"rule_a": self.rule_a, "rule_b": self.rule_b, "witness": self.witness}


@dataclass(frozen=True)
class ReachabilityEntry:
    rule: str
    unreachable: bool
    witness: str | None  # 可达时：一个只会被本规则赢得的串

    def to_dict(self) -> dict:
        return {"rule": self.rule, "unreachable": self.unreachable, "witness": self.witness}


def overlap_report(nfas: list[NFA], rules: list[Rule], limits: Limits, logger: RunLogger) -> list[OverlapEntry]:
    entries: list[OverlapEntry] = []
    checked = 0
    for i in range(len(rules)):
        for j in range(i + 1, len(rules)):
            checked += 1
            witness = shortest_shared_string(nfas[i], nfas[j], limits)
            if witness is not None:
                entries.append(OverlapEntry(rules[i].name, rules[j].name, witness))
    logger.log(
        MODULE,
        "overlap_checked",
        state={"pairs_checked": checked, "overlapping": len(entries)},
        rationale="逐对构造积自动机并以 0-1 BFS 求交集最短串作为见证",
    )
    return entries


def _beats(i: int, rules: list[Rule]) -> list[int]:
    """等长时能击败规则 i 的规则下标：优先级更高，或同优先级声明在前。"""
    out = []
    for j, r in enumerate(rules):
        if j == i:
            continue
        if (r.priority, -r.index) > (rules[i].priority, -rules[i].index):
            out.append(j)
    return out


def unreachable_report(nfas: list[NFA], rules: list[Rule], limits: Limits, logger: RunLogger) -> list[ReachabilityEntry]:
    entries: list[ReachabilityEntry] = []
    for i, rule in enumerate(rules):
        beats = _beats(i, rules)
        if not beats:
            witness = shortest_shared_string(nfas[i], universal_nfa(), limits)
            entries.append(ReachabilityEntry(rule.name, unreachable=witness is None, witness=witness))
            continue
        union = combine_nfas([nfas[j] for j in beats], limits)
        union_dfa = complete_dfa(nfa_to_dfa(union, limits))
        complement = complement_dfa(union_dfa)
        complement_nfa = NFA(
            nstates=complement.nstates,
            start=complement.start,
            accepts={s: t for s, tags in enumerate(complement.accepts) for t in tags},
            eps=[[] for _ in range(complement.nstates)],
            trans=[
                [(CharSet.normalize([(lo, hi)]), tgt) for lo, hi, tgt in edges]
                for edges in complement.trans
            ],
        )
        witness = shortest_shared_string(nfas[i], complement_nfa, limits)
        unreachable = witness is None
        entries.append(ReachabilityEntry(rule.name, unreachable=unreachable, witness=witness))
        logger.log(
            MODULE,
            "reachability_checked",
            state={
                "rule": rule.name,
                "beaten_by": [rules[j].name for j in beats],
                "unreachable": unreachable,
                "witness": witness,
            },
            rationale="L(R) ∩ 补(⋃L(击败者)) 为空则规则永不能胜出，判为不可达",
        )
    return entries
