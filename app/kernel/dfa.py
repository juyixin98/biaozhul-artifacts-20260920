"""DFA：子集构造（按字母表划分单元转移）、完备化、补。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Limits
from app.errors import ResourceExhausted
from app.kernel.charset import CharSet, partition_alphabet
from app.kernel.nfa import NFA, epsilon_closure

COMPLEMENT_TAG = -1  # 补自动机中标记“原自动机不接受”的哨兵标签


@dataclass
class DFA:
    nstates: int
    start: int
    # 每状态：排序不相交区间 (lo, hi, target)；完备化后覆盖全字母表
    trans: list[list[tuple[int, int, int]]] = field(default_factory=list)
    accepts: list[frozenset[int]] = field(default_factory=list)  # 每状态接受的规则标签集

    def find_edge(self, state: int, cp: int) -> int | None:
        for lo, hi, tgt in self.trans[state]:
            if cp < lo:
                return None
            if lo <= cp <= hi:
                return tgt
        return None

    def to_dict(self) -> dict:
        return {
            "nstates": self.nstates,
            "start": self.start,
            "trans": {str(s): [[lo, hi, t] for lo, hi, t in edges] for s, edges in enumerate(self.trans)},
            "accepts": {str(s): sorted(tags) for s, tags in enumerate(self.accepts) if tags},
        }

    @staticmethod
    def from_dict(d: dict) -> "DFA":
        n = d["nstates"]
        trans: list[list[tuple[int, int, int]]] = [[] for _ in range(n)]
        for s, edges in d["trans"].items():
            trans[int(s)] = [(lo, hi, t) for lo, hi, t in edges]
        accepts: list[frozenset[int]] = [frozenset() for _ in range(n)]
        for s, tags in d["accepts"].items():
            accepts[int(s)] = frozenset(tags)
        return DFA(nstates=n, start=d["start"], trans=trans, accepts=accepts)


def nfa_to_dfa(nfa: NFA, limits: Limits) -> DFA:
    all_charsets = [cs for edges in nfa.trans for cs, _ in edges]
    cells = partition_alphabet(all_charsets)

    state_ids: dict[frozenset[int], int] = {}
    subsets: list[frozenset[int]] = []
    trans: list[list[tuple[int, int, int]]] = []
    accepts: list[frozenset[int]] = []

    def intern(subset: frozenset[int]) -> int:
        if subset in state_ids:
            return state_ids[subset]
        if len(subsets) >= limits.max_dfa_states:
            raise ResourceExhausted(
                code="DFA_TOO_LARGE",
                message=f"DFA 状态数超过上限 {limits.max_dfa_states}",
                details={"limit": limits.max_dfa_states},
            )
        sid = len(subsets)
        state_ids[subset] = sid
        subsets.append(subset)
        trans.append([])
        accepts.append(frozenset(nfa.accepts[s] for s in subset if s in nfa.accepts))
        return sid

    intern(epsilon_closure(nfa.eps, {nfa.start}))
    sid = 0
    while sid < len(subsets):
        subset = subsets[sid]
        # 划分单元 -> 目标子集（单元细于所有字符集，要么全含要么不含）
        cell_target: dict[int, frozenset[int]] = {}
        for cell in cells:
            (lo, _), = cell.intervals
            moved: set[int] = set()
            for s in subset:
                for cs, tgt in nfa.trans[s]:
                    if not cs.intersect(cell).is_empty():
                        moved.add(tgt)
            if moved:
                cell_target[lo] = epsilon_closure(nfa.eps, moved)
        # 合并相邻且目标相同的单元，压缩转移表
        edges: list[tuple[int, int, int]] = []
        for cell in cells:
            (lo, hi), = cell.intervals
            tgt_subset = cell_target.get(lo)
            if tgt_subset is None:
                continue
            tgt = intern(tgt_subset)
            if edges and edges[-1][2] == tgt and edges[-1][1] == lo - 1:
                edges[-1] = (edges[-1][0], hi, tgt)
            else:
                edges.append((lo, hi, tgt))
        trans[sid] = edges
        sid += 1
    return DFA(nstates=len(subsets), start=0, trans=trans, accepts=accepts)


def complete_dfa(dfa: DFA) -> DFA:
    """完备化：缺失转移补到汇态（自环）。返回新 DFA。"""
    trans = [list(edges) for edges in dfa.trans]
    accepts = list(dfa.accepts)
    full = CharSet.full()
    sink = len(trans)
    trans.append([])
    accepts.append(frozenset())
    needed = False
    for sid in range(len(trans) - 1):
        covered = CharSet.normalize([(lo, hi) for lo, hi, _ in trans[sid]])
        missing = full.subtract(covered)
        if not missing.is_empty():
            needed = True
            for lo, hi in missing.intervals:
                trans[sid].append((lo, hi, sink))
            trans[sid].sort()
    if not needed:
        return DFA(nstates=len(trans) - 1, start=dfa.start, trans=trans[:-1], accepts=accepts[:-1])
    trans[sink] = [(lo, hi, sink) for lo, hi in full.intervals]
    return DFA(nstates=len(trans), start=dfa.start, trans=trans, accepts=accepts)


def complement_dfa(dfa: DFA) -> DFA:
    """补：接受集取反。调用方须先完备化。

    补接受态以哨兵标签 COMPLEMENT_TAG 标记，原接受态变为不接受。
    """
    accepts = [
        frozenset() if tags else frozenset({COMPLEMENT_TAG}) for tags in dfa.accepts
    ]
    return DFA(
        nstates=dfa.nstates,
        start=dfa.start,
        trans=[list(e) for e in dfa.trans],
        accepts=accepts,
    )
