#!/usr/bin/env python3
"""验收基准: 合成随机稀疏数据上, 倒排索引(WAND 剪枝) vs 全扫描。

做什么
------
1. 用固定种子生成含负权重、重复维度、零向量文档的合成语料;
2. 对每条查询分别用 :func:`brute_force_topk` 与 :class:`InvertedIndex`
   检索, 逐条核对 doc_id 与分数**完全一致**;
3. 如实报告: 实际计算余弦的候选数、跳过次数、剪枝率、耗时。

用法::

    python scripts/benchmark.py            # 默认 5000 文档
    python scripts/benchmark.py --n-docs 20000 --dim 2000 --k 10
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sparse_retrieval.data import generate_dataset  # noqa: E402
from sparse_retrieval.index import InvertedIndex  # noqa: E402
from sparse_retrieval.scoring import brute_force_topk  # noqa: E402
from sparse_retrieval.vector import SparseVector  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="WAND 倒排索引 vs 全扫描验收基准")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--n-docs", type=int, default=5000)
    p.add_argument("--n-queries", type=int, default=100)
    p.add_argument("--dim", type=int, default=1000)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--negative-prob", type=float, default=0.25)
    p.add_argument("--n-zero-docs", type=int, default=20)
    p.add_argument("--duplicate-prob", type=float, default=0.2)
    p.add_argument(
        "--query-kind",
        choices=["topic", "rare", "zero"],
        default="topic",
        help="topic=主题相关查询; rare=随机冷僻维度(命中少, 触发 0/负分兜底); "
        "zero=零向量查询",
    )
    return p.parse_args()


def build_queries(
    kind: str, ds: Any, args: argparse.Namespace
) -> list[SparseVector]:
    if kind == "topic":
        return ds.queries
    rng = np.random.default_rng(args.seed + 10_000)
    if kind == "zero":
        return [SparseVector.create([], [], args.dim) for _ in range(args.n_queries)]
    queries = []
    for _ in range(args.n_queries):
        nnz = int(rng.integers(1, 4))  # 故意很短, 倒排命中少
        idx = rng.choice(args.dim, size=nnz, replace=False)
        val = rng.normal(0.0, 1.0, size=nnz)
        val *= np.where(rng.random(nnz) < 0.5, -1.0, 1.0)
        queries.append(SparseVector.create(idx.tolist(), val.tolist(), args.dim))
    return queries


def main() -> int:
    args = parse_args()

    print("=" * 72)
    print("稀疏向量余弦检索: 倒排索引(WAND 剪枝) vs 全扫描")
    print("=" * 72)
    t0 = time.perf_counter()
    ds = generate_dataset(
        seed=args.seed,
        n_docs=args.n_docs,
        n_queries=args.n_queries,
        dim=args.dim,
        negative_prob=args.negative_prob,
        n_zero_docs=args.n_zero_docs,
        duplicate_prob=args.duplicate_prob,
    )
    gen_s = time.perf_counter() - t0

    zero_docs = sum(1 for d in ds.docs if d.nnz == 0)
    nnz = np.array([d.nnz for d in ds.docs])
    neg_slots = sum(int(np.sum(d.values < 0)) for d in ds.docs)
    print(f"语料: {len(ds.docs)} 文档, dim={args.dim}, seed={args.seed}")
    print(f"  文档 nnz: min={nnz.min()}, mean={nnz.mean():.1f}, max={nnz.max()}")
    print(f"  零向量文档: {zero_docs}; 含负权重的维度槽位: {neg_slots}")
    print(f"  数据生成耗时: {gen_s:.3f}s")

    t0 = time.perf_counter()
    index = InvertedIndex(ds.docs)
    build_s = time.perf_counter() - t0
    chain_lens = [len(d) for d, _ in index.postings.values()]
    print(f"索引: {len(index.postings)} 条倒排链, 建库耗时 {build_s:.3f}s, "
          f"链长 mean={np.mean(chain_lens):.1f}, max={max(chain_lens)}")
    print("-" * 72)

    queries = build_queries(args.query_kind, ds, args)
    print(f"查询类型: {args.query_kind}, 查询数: {len(queries)}, k={args.k}")

    mismatches = 0
    scan_times: list[float] = []
    wand_times: list[float] = []
    scored_counts: list[int] = []
    skip_counts: list[int] = []
    fallback_queries = 0

    for qi, query in enumerate(queries):
        t0 = time.perf_counter()
        expected = brute_force_topk(query, ds.docs, args.k)
        scan_times.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        result = index.search(query, k=args.k)
        wand_times.append(time.perf_counter() - t0)

        got = [(h.doc_id, h.score) for h in result.hits]
        if got != expected:
            mismatches += 1
            print(f"[不一致] 查询 {qi}:")
            print("  索引:", got[:5])
            print("  扫描:", expected[:5])

        scored_counts.append(result.candidates_scored)
        skip_counts.append(result.wand_pivot_skips)
        if result.fallback_used:
            fallback_queries += 1

    n = len(queries)
    scored = np.asarray(scored_counts)
    skips = np.asarray(skip_counts)
    print(f"精确性: 索引与全扫描结果{'完全一致 ✅' if mismatches == 0 else str(mismatches) + ' 条不一致 ❌'}")
    print(f"  触发负分/零分兜底的查询: {fallback_queries}/{n}")
    print("-" * 72)
    print("实际候选数(计算了余弦的文档数, 全扫描基准 = "
          f"{args.n_docs}/查询):")
    print(f"  min={scored.min()}  mean={scored.mean():.1f}  "
          f"p50={int(np.percentile(scored, 50))}  "
          f"p95={int(np.percentile(scored, 95))}  max={scored.max()}")
    print(f"  平均候选占比: {scored.mean() / args.n_docs:.2%}  "
          f"(平均剪枝率 {1 - scored.mean() / args.n_docs:.2%})")
    print(f"  WAND 枢轴跳次: total={int(skips.sum())}, "
          f"mean={skips.mean():.1f}/查询")
    print("-" * 72)
    print(f"全扫描总耗时: {sum(scan_times):.4f}s, "
          f"均值 {np.mean(scan_times) * 1000:.3f} ms/查询")
    print(f"索引检索总耗时: {sum(wand_times):.4f}s, "
          f"均值 {np.mean(wand_times) * 1000:.3f} ms/查询")
    print(f"加速比(仅检索, 不含建库): {np.mean(scan_times) / np.mean(wand_times):.2f}x")
    print("=" * 72)

    edge_mismatches = run_edge_case()
    total_mismatches = mismatches + edge_mismatches
    return 1 if total_mismatches else 0


def run_edge_case() -> int:
    """手工构造的边界语料: 强制触发 0/负分兜底与并列。

    - 仅 1 个正分命中文档;
    - 多个负分命中文档;
    - 多个无公共维度的 0 分文档与零向量文档;
    - 重复维度在构造时合并。
    """
    print("边界场景(正分命中 < k, 含负分/0 分/零向量/重复维度)")
    dim = 6
    docs = [
        SparseVector.create([0, 0, 1], [2.0, 1.0, 1.0], dim),   # 正分赢家(重复维合并)
        SparseVector.create([0, 2], [-1.0, 1.0], dim),          # 负分
        SparseVector.create([0], [-3.0], dim),                  # 负分
        SparseVector.create([1], [1.0], dim),                   # 正分但弱
        SparseVector.create([3, 4], [1.0, 1.0], dim),           # 无交集 0 分
        SparseVector.create([5], [2.0], dim),                   # 无交集 0 分
        SparseVector.create([], [], dim),                       # 零向量文档
        SparseVector.create([2, 2], [1.0, -1.0], dim),          # 重复抵消 -> 零向量
    ]
    index = InvertedIndex(docs)
    query = SparseVector.create([0, 1], [1.0, 1.0], dim)
    mismatches = 0
    for k in (1, 3, 5, 8, 20):
        result = index.search(query, k=k)
        expected = brute_force_topk(query, docs, k)
        got = [(h.doc_id, round(h.score, 12)) for h in result.hits]
        want = [(d, round(s, 12)) for d, s in expected]
        ok = got == want
        mismatches += int(not ok)
        print(f"  k={k:2d}: {'✅' if ok else '❌'} "
              f"候选={result.candidates_scored}/{index.n_docs}, "
              f"0分补位={result.zero_filled}")
        print(f"       名次: {got}")
        if not ok:
            print(f"       期望: {want}")
    print("=" * 72)
    return mismatches


if __name__ == "__main__":
    raise SystemExit(main())
