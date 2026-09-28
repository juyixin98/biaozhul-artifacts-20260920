"""重叠诊断：最短见证的具体值、独立自动机交集核验、re 预言机复核。

独立性说明：
- 见证归属用 Python re（与内核无关的实现）复核；
- 最短性用暴力枚举（短于见证的所有串都不属于交集）证明；
- 另用测试本地的积自动机 BFS（作用于内核导出的 DFA 数据）独立复核，
  搜索算法不是被测内核的代码。
"""

import re
from collections import deque

from app.kernel.diagnostics import overlap_report
from app.kernel.lexer import Rule
from app.kernel.nfa import ast_to_nfa
from app.kernel.regex_ast import parse_pattern

from conftest import iter_strings

OVERLAP_SPEC = [
    ("R1", "ab*c", 0),
    ("R2", "a[bc]+", 0),
    ("IF", "if", 10),
    ("IDENT", "[A-Za-z_][A-Za-z0-9_]*", 0),
]


def _compiled(spec, limits):
    rules = [Rule(i, n, p, prio, skip=False) for i, (n, p, prio) in enumerate(spec)]
    nfas = [ast_to_nfa(parse_pattern(r.pattern, limits), tag=r.index, limits=limits) for r in rules]
    return rules, nfas


def test_overlap_witness_exact_values(limits, logger):
    rules, nfas = _compiled(OVERLAP_SPEC, limits)
    report = overlap_report(nfas, rules, limits, logger)
    by_pair = {(e.rule_a, e.rule_b): e.witness for e in report}
    # 手工参考：ab*c ∩ a[bc]+ 的最短公共串是 "ac"；if ∩ IDENT 是 "if"
    assert by_pair[("R1", "R2")] == "ac"
    assert by_pair[("IF", "IDENT")] == "if"
    # 不相交对不应出现：ab*c 与 if 无交集
    assert ("R1", "IF") not in by_pair


def test_witness_verified_by_independent_oracle(limits, logger):
    rules, nfas = _compiled(OVERLAP_SPEC, limits)
    report = overlap_report(nfas, rules, limits, logger)
    patterns = {r.name: r.pattern for r in rules}
    for entry in report:
        # re 预言机：见证同时属于两个语言
        assert re.fullmatch(patterns[entry.rule_a], entry.witness)
        assert re.fullmatch(patterns[entry.rule_b], entry.witness)
        # 暴力最短性：更短的串都不在交集中（字母表取两模式出现的字符）
        alphabet = sorted(set("abcif_"))
        for s in iter_strings("".join(alphabet), len(entry.witness) - 1):
            assert not (
                re.fullmatch(patterns[entry.rule_a], s)
                and re.fullmatch(patterns[entry.rule_b], s)
            ), f"更短公共串 {s!r} 反驳了见证 {entry.witness!r} 的最短性"


def _dfa_product_shortest(dfa_a: dict, dfa_b: dict) -> str | None:
    """测试本地实现：在导出的 DFA 数据上做积自动机 BFS（不调用内核诊断代码）。"""
    start = (dfa_a["start"], dfa_b["start"])
    accepts_a = {int(s) for s in dfa_a["accepts"]}
    accepts_b = {int(s) for s in dfa_b["accepts"]}
    trans_a = {int(s): e for s, e in dfa_a["trans"].items()}
    trans_b = {int(s): e for s, e in dfa_b["trans"].items()}
    queue = deque([(start, "")])
    seen = {start}
    while queue:
        (sa, sb), s = queue.popleft()
        if sa in accepts_a and sb in accepts_b:
            return s
        for lo_a, hi_a, ta in trans_a.get(sa, []):
            for lo_b, hi_b, tb in trans_b.get(sb, []):
                lo, hi = max(lo_a, lo_b), min(hi_a, hi_b)
                if lo > hi:
                    continue
                nxt = (ta, tb)
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, s + chr(lo)))
    return None


def test_witness_confirmed_by_independent_automaton_intersection(limits, logger):
    from app.kernel.dfa import nfa_to_dfa

    rules, nfas = _compiled(OVERLAP_SPEC, limits)
    report = {(e.rule_a, e.rule_b): e.witness for e in overlap_report(nfas, rules, limits, logger)}
    dfas = {r.name: nfa_to_dfa(nfa, limits).to_dict() for r, nfa in zip(rules, nfas)}
    for (a, b), witness in report.items():
        independent = _dfa_product_shortest(dfas[a], dfas[b])
        assert independent is not None, f"{a}∩{b}: 独立交集未找到公共串"
        assert len(independent) == len(witness), (
            f"{a}∩{b}: 内核见证 {witness!r} 与独立交集最短串 {independent!r} 长度不一致"
        )
