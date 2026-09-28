"""挖掘内核：从对齐语料估计词典映射与字符/词形规则的代价。

估计方法（明确、可复核）：

* **词典代价**：同一输入串的多个输出按出现计数做加一平滑的多项分布，
  ``cost(in->out) = -ln((c + α) / (Σc' + α·K))``，K 为该输入的不同输出数。
* **编辑规则代价**：先用一个**独立实现的** Levenshtein 动态规划回溯出
  字符级对齐（sub/ins/del/identity），汇总各类操作计数后同样按
  加类别平滑的多项分布估代价（按操作类型分组归一化）。

该模块只产出普通数据结构（:class:`MinedArc` 列表等），不导入组合/最短
路径算法，因此测试可以把它作为独立参考实现交叉核对 FST 核心结果，
满足「参考答案不能全部由被测核心自身生成」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from wfst.corpus.spec import CorpusSpec

EPSILON = "<eps>"


class Op(str, Enum):
    SUB = "sub"
    INS = "ins"
    DEL = "del"
    IDENTITY = "identity"


@dataclass(frozen=True, slots=True)
class MinedArc:
    """挖掘出的一条映射/规则弧（纯数据，与 FST 核心解耦）。"""

    ilabel: str
    olabel: str
    op: Op
    cost: float
    count: int


@dataclass(frozen=True, slots=True)
class LexiconEntry:
    text_in: str
    text_out: str
    cost: float
    count: int


@dataclass(slots=True)
class MiningReport:
    """挖掘过程的可复核记录（步骤、计数、代价依据）。"""

    corpus_id: str
    version: str
    lexicon_entries: list[LexiconEntry] = field(default_factory=list)
    arcs: list[MinedArc] = field(default_factory=list)
    op_counts: dict[str, int] = field(default_factory=dict)
    pair_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    smoothing: float = 0.0

    def steps(self) -> list[str]:
        lines = [
            f"语料 {self.corpus_id}@{self.version}："
            f"{len(self.pair_counts)} 个不同对齐对，平滑 α={self.smoothing}",
        ]
        for op, n in sorted(self.op_counts.items()):
            lines.append(f"  操作 {op}: 计数 {n}")
        return lines


def _smoothed_cost(count: int, total: int, alternatives: int, alpha: float) -> float:
    """加 α 平滑的负对数似然代价。"""
    denom = total + alpha * max(alternatives, 1)
    prob = (count + alpha) / denom
    return -math.log(prob)


# ---------- 独立的 Levenshtein 对齐（参考实现，不经过 FST） ----------

def levenshtein_alignment(a: str, b: str) -> list[tuple[Op, str, str]]:
    """用经典 DP 回溯出一条编辑脚本（独立参考实现）。

    返回 ``(op, ilabel, olabel)`` 列表；插入时 ilabel=``<eps>``，
    删除时 olabel=``<eps>``。并列最优选恒等 > 替换 > 删除/插入 的稳定
    回溯顺序，保证结果确定。
    """
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j - 1] + cost,
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
            )

    script: list[tuple[Op, str, str]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            cost = 0 if a[i - 1] == b[j - 1] else 1
            if dp[i][j] == dp[i - 1][j - 1] + cost:
                if a[i - 1] == b[j - 1]:
                    script.append((Op.IDENTITY, a[i - 1], b[j - 1]))
                else:
                    script.append((Op.SUB, a[i - 1], b[j - 1]))
                i -= 1
                j -= 1
                continue
        if i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            script.append((Op.DEL, a[i - 1], EPSILON))
            i -= 1
        else:
            # 插入（回溯末选，保证确定性）。
            script.append((Op.INS, EPSILON, b[j - 1]))
            j -= 1
    script.reverse()
    return script


def mine_corpus(spec: CorpusSpec, smoothing: float = 0.5) -> MiningReport:
    """从语料规范挖掘词典条目与编辑规则，返回带计数依据的报告。"""
    if smoothing < 0:
        raise ValueError("平滑系数不能为负")
    report = MiningReport(
        corpus_id=spec.corpus_id, version=spec.version, smoothing=smoothing
    )

    # 1) 词典映射仅收录「非编辑标签」的对齐（拼写对只用于训练编辑代价，
    #    不应作为整串词典键，否则错拼形式会以零纠错代价被直接接受）。
    edit_tags = set(spec.edit_tags)
    lex_alignments = [
        al for al in spec.alignments if not edit_tags or al.tag not in edit_tags
    ]
    pair_counts: dict[tuple[str, str], int] = {}
    in_totals: dict[str, int] = {}
    in_alternatives: dict[str, set[str]] = {}
    for al in lex_alignments:
        key = (al.input, al.output)
        pair_counts[key] = pair_counts.get(key, 0) + al.count
        in_totals[al.input] = in_totals.get(al.input, 0) + al.count
        in_alternatives.setdefault(al.input, set()).add(al.output)
    report.pair_counts = dict(pair_counts)

    for (text_in, text_out), c in pair_counts.items():
        cost = _smoothed_cost(
            c, in_totals[text_in], len(in_alternatives[text_in]), smoothing
        )
        report.lexicon_entries.append(
            LexiconEntry(text_in, text_out, cost=cost, count=c)
        )

    # 2) 字符/词形编辑操作计数（仅统计 edit_tags 标记的对齐，词典义项
    #    不参与，避免把整串词典映射误当字符编辑）。
    tokenize = (lambda s: tuple(s)) if spec.token_level == "char" else (
        lambda s: tuple(s.split())
    )
    edit_tags = set(spec.edit_tags)
    op_key_counts: dict[tuple[Op, str, str], int] = {}
    op_totals: dict[Op, int] = {op: 0 for op in Op}
    op_alternatives: dict[Op, set[tuple[str, str]]] = {op: set() for op in Op}
    edit_alignments = [
        al for al in spec.alignments if not edit_tags or al.tag in edit_tags
    ]
    for al in edit_alignments:
        src_tokens = list(tokenize(al.input))
        dst_tokens = list(tokenize(al.output))
        for op, il, ol in levenshtein_alignment(src_tokens, dst_tokens):
            key = (op, il, ol)
            op_key_counts[key] = op_key_counts.get(key, 0) + al.count
            op_totals[op] += al.count
            op_alternatives[op].add((il, ol))

    for (op, il, ol), c in sorted(op_key_counts.items()):
        cost = _smoothed_cost(
            c, op_totals[op], len(op_alternatives[op]), smoothing
        )
        report.arcs.append(MinedArc(il, ol, op, cost=cost, count=c))
    report.op_counts = {op.value: n for op, n in op_totals.items() if n}

    # 注意：语料显式声明的 rules（spec.rules）不混入挖掘出的编辑弧，
    # 它们由模型层单独编译为「词形规则级」，与挖掘出的编辑级分离。
    report.lexicon_entries.sort(key=lambda e: (e.text_in, e.cost, e.text_out))
    return report
