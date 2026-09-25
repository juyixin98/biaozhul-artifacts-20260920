"""稀疏余弦检索倒排索引(带精确剪枝)。

索引结构
--------
- ``postings[t]``: 维度 ``t`` 的倒排链 ``(doc_ids 升序, weights)``;
- ``doc_norms[d]``: 文档 L2 范数(建库时预算);
- ``upper_bounds[t]``: 维度级上界 ``max_d |w(t,d)| / ||d||``。

WAND 剪枝
~~~~~~~~~
对查询项 t, 其上界贡献为 ``a_t = |q_t|/||q|| * upper_bounds[t] >= 0``
(取绝对值, 因此**负权重不会破坏上界有效性**)。WAND 按游标文档号
排序、累加 a_t 找到枢轴项: 枢轴之前的贡献之和都不足阈值 θ 时,
该候选不可能进入 TopK, 直接跳过整条链的部分文档, 且**永不改变精确
结果**——每个被剪枝的文档都有 ``真实分数 <= 上界和 < θ``。

零分与负分的处理
~~~~~~~~~~~~~~~~
余弦可能为负、可能恰好为 0(负权重抵消、文档与查询无公共维度、零向量
文档)。由于上界 a_t 非负, 当当前第 k 名阈值 θ <= 0 时, 排序后的第一个
游标就满足 cum >= θ, WAND 退化为多路归并——**每个命中文档都被精确
打分**, 不会跳过任何 0/负分文档; 无公共维度的文档分数必为 0, 再按
doc_id 升序与已打分文档归并补位。只有 θ > 0(TopK 已被正分填满)后
才会跳过文档, 而被跳过文档上界和都 < θ, 必然是负分, 与名次无关。
最终答案一律由精确分数经 ``(分数降序, ID 升序)`` 决断, 剪枝只影响
"算了多少个余弦", 不影响任何一个名次。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

import numpy as np

from .scoring import sparse_dot
from .vector import SparseVector, l2_norm

__all__ = ["IndexError", "ScoredDoc", "SearchResult", "InvertedIndex"]


class IndexError(ValueError):
    """索引构建/检索参数错误。"""


@dataclass(frozen=True)
class ScoredDoc:
    doc_id: int
    score: float


@dataclass(frozen=True)
class SearchResult:
    """一次检索的结果与可观测的剪枝统计。"""

    hits: tuple[ScoredDoc, ...]
    n_docs: int
    query_nnz: int
    candidates_scored: int = 0
    wand_pivot_skips: int = 0
    zero_filled: int = 0

    @property
    def fallback_used(self) -> bool:
        """TopK 未被正分文档填满, 使用了 0 分文档补位。"""
        return self.zero_filled > 0

    @property
    def candidates_considered(self) -> int:
        """进入最终名次裁决的候选总数(含 0 分补位文档)。"""
        return self.candidates_scored + self.zero_filled

    def stats_dict(self) -> dict:
        return {
            "n_docs": self.n_docs,
            "query_nnz": self.query_nnz,
            "candidates_scored": self.candidates_scored,
            "wand_pivot_skips": self.wand_pivot_skips,
            "fallback_used": self.fallback_used,
            "zero_filled": self.zero_filled,
            "candidates_considered": self.candidates_considered,
            "full_scan_docs": self.n_docs,
            "pruning_ratio": (
                1.0 - self.candidates_scored / self.n_docs if self.n_docs else 0.0
            ),
        }


class _Cursor:
    """倒排链上的单调游标。"""

    __slots__ = ("doc_ids", "upper", "pos")

    def __init__(
        self,
        doc_ids: np.ndarray,
        upper: float | np.floating,
    ) -> None:
        self.doc_ids = doc_ids
        # 转 longdouble 累加, 且构造时已 nextafter 上抬, 保证严格保守
        self.upper = np.longdouble(upper)
        self.pos = 0

    def doc(self) -> int:
        return int(self.doc_ids[self.pos]) if self.pos < len(self.doc_ids) else -1

    def exhausted(self) -> bool:
        return self.pos >= len(self.doc_ids)

    def next_ge(self, target: int) -> None:
        """前进到第一个 doc_id >= target 的位置。"""
        if self.exhausted() or self.doc() >= target:
            return
        self.pos += int(
            np.searchsorted(self.doc_ids[self.pos :], target, side="left")
        )


class InvertedIndex:
    """维度一致的稀疏向量倒排索引。"""

    def __init__(self, docs: list[SparseVector]) -> None:
        if not docs:
            raise IndexError("至少需要一个文档")
        self.dim = docs[0].dim
        for d in docs:
            if d.dim != self.dim:
                raise IndexError("所有文档维度必须一致")
        self.docs: list[SparseVector] = list(docs)
        self.n_docs = len(docs)
        self.doc_norms = np.array([d.norm for d in docs], dtype=np.float64)
        self._build()

    def _build(self) -> None:
        buckets: dict[int, list[tuple[int, float]]] = {}
        for doc_id, vec in enumerate(self.docs):
            for t, w in zip(vec.indices, vec.values):
                buckets.setdefault(int(t), []).append((doc_id, float(w)))
        self.postings: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        self.upper_bounds: dict[int, float] = {}
        for term, entries in buckets.items():
            entries.sort(key=lambda e: e[0])
            doc_ids = np.array([e[0] for e in entries], dtype=np.int64)
            weights = np.array([e[1] for e in entries], dtype=np.float64)
            self.postings[term] = (doc_ids, weights)
            norms = self.doc_norms[doc_ids]
            # 单向向上取整 1 ulp: 抵消浮点舍入, 保证上界恒 >= 任何真实贡献
            bound = np.max(np.abs(weights) / norms)
            self.upper_bounds[term] = float(np.nextafter(bound, np.inf))

    # ------------------------------------------------------------------ #
    def _exact_score(self, doc_id: int, qnorm: float, query: SparseVector) -> float:
        """与全扫描基准完全相同的计算路径(升序维度点积), 逐位一致。"""
        dnorm = self.doc_norms[doc_id]
        if dnorm == 0.0:
            return 0.0
        return sparse_dot(query, self.docs[doc_id]) / (qnorm * dnorm)

    def _zero_result(self, k: int, query_nnz: int) -> SearchResult:
        take = min(k, self.n_docs)
        return SearchResult(
            hits=tuple(ScoredDoc(d, 0.0) for d in range(take)),
            n_docs=self.n_docs,
            query_nnz=query_nnz,
            zero_filled=take,
        )

    def search(self, query: SparseVector, k: int = 10) -> SearchResult:
        if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
            raise IndexError("k 必须是正整数")
        if query.dim != self.dim:
            raise IndexError(
                f"查询维度 {query.dim} 与索引维度 {self.dim} 不一致"
            )

        # 零向量查询 / 查询项无倒排链: 所有分数定义为 0, ID 升序取前 k。
        if query.nnz == 0:
            return self._zero_result(k, 0)

        qnorm = l2_norm(query.values)
        cursors = self._make_cursors(query, qnorm)
        if not cursors:
            return self._zero_result(k, query.nnz)

        heap: list[tuple[float, int, int]] = []  # (score, -doc_id, doc_id)
        scored_count = 0
        pivot_skips = 0

        def consider(doc_id: int) -> None:
            nonlocal scored_count
            score = self._exact_score(doc_id, qnorm, query)
            scored_count += 1
            if len(heap) < k:
                heapq.heappush(heap, (score, -doc_id, doc_id))
            elif (score, -doc_id) > (heap[0][0], heap[0][1]):
                heapq.heapreplace(heap, (score, -doc_id, doc_id))

        theta = float("-inf")

        # 标准 DAAT-WAND 主循环(Broder et al., 2003 的枢轴策略)。
        # 上界 a_t >= 0: 当 θ <= 0 时首个游标即满足 cum >= θ,
        # 循环退化为 k 路归并, 每个命中文档都被精确打分; 只有 θ > 0
        # 后才会发生跳过, 而被跳过文档的上界和 < θ, 必非 TopK。
        while True:
            alive = [c for c in cursors if not c.exhausted()]
            if not alive:
                break
            alive.sort(key=lambda c: c.doc())

            cum = 0.0
            pivot_idx = -1
            for i, c in enumerate(alive):
                cum += c.upper
                if cum >= theta:
                    pivot_idx = i
                    break
            if pivot_idx < 0:
                break

            pivot = alive[pivot_idx]
            pivot_doc = pivot.doc()
            prefix_at_doc = (
                pivot_idx == 0 or alive[pivot_idx - 1].doc() == pivot_doc
            )
            if prefix_at_doc:
                # 枢轴文档上界可达阈值: 精确打分(分数取自档向量,
                # 不依赖游标位置), 再移走所有停在该文档上的游标。
                consider(pivot_doc)
                for c in alive:
                    if c.doc() == pivot_doc:
                        c.next_ge(pivot_doc + 1)
                theta = heap[0][0] if len(heap) == k else float("-inf")
            else:
                # 枢轴之前各项的上界之和仍 < θ: 严格小于 pivot_doc 的
                # 所有文档必败, 把前缀链一次性前移到 pivot_doc。
                pivot_skips += 1
                for c in alive[:pivot_idx]:
                    c.next_ge(pivot_doc)

        # θ > 0 时 TopK 已被正分文档填满, 被剪枝文档必为负分, 无关名次。
        # θ <= 0 时所有命中文档都已精确打分, 堆中是其中的 TopK;
        # 无公共维度的文档必然 0 分, 与其按 ID 升序归并补位,
        # 等价于全扫描中 0 分并列的稳定次序。
        in_union = np.zeros(self.n_docs, dtype=bool)
        for c in cursors:
            if len(c.doc_ids):
                in_union[c.doc_ids] = True

        scored = [(s, d) for s, _, d in heap]
        scored.sort(key=lambda x: (-x[0], x[1]))

        filler_ids = np.flatnonzero(~in_union)
        chosen: list[tuple[float, int]] = []
        si = 0
        fi = 0
        while len(chosen) < k and (si < len(scored) or fi < filler_ids.size):
            if fi >= filler_ids.size:
                chosen.append(scored[si])
                si += 1
            elif si >= len(scored):
                chosen.append((0.0, int(filler_ids[fi])))
                fi += 1
            else:
                s_score, s_id = scored[si]
                f_id = int(filler_ids[fi])
                if (-s_score, s_id) <= (0.0, f_id):
                    chosen.append((s_score, s_id))
                    si += 1
                else:
                    chosen.append((0.0, f_id))
                    fi += 1
        zero_filled = fi

        hits = tuple(ScoredDoc(d, float(s)) for s, d in chosen[:k])
        return SearchResult(
            hits=hits,
            n_docs=self.n_docs,
            query_nnz=query.nnz,
            candidates_scored=scored_count,
            wand_pivot_skips=pivot_skips,
            zero_filled=zero_filled,
        )

    def _make_cursors(self, query: SparseVector, qnorm: float) -> list[_Cursor]:
        cursors: list[_Cursor] = []
        # 在 longdouble 中做乘积, 配合 nextafter 过的维度上界保证严格保守
        scale = np.longdouble(1.0) / np.longdouble(qnorm)
        for t, w in zip(query.indices, query.values):
            posting = self.postings.get(int(t))
            if posting is None:
                continue
            doc_ids, _ = posting
            upper = np.longdouble(abs(float(w))) * scale * np.longdouble(
                self.upper_bounds[int(t)]
            )
            cursors.append(_Cursor(doc_ids, upper))
        return cursors
