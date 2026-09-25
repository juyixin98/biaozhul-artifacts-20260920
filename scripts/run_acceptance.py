#!/usr/bin/env python3
"""Acceptance run: inverted index vs full scan on random sparse data.

Compares exact TopK results (bitwise) between the inverted index, a sparse
full scan, and a NumPy dense reference. Reports the actual candidate counts
the index examined. Exits non-zero on any mismatch.

Usage: python3 scripts/run_acceptance.py [--seed N] [--docs N] [--dim N]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sparse_retrieval import InvertedIndex, SparseVector, brute_force_query
from sparse_retrieval.synthetic import random_corpus, random_queries, to_dense


def dense_topk(docs, doc_ids, q, dim, k):
    """NumPy dense full scan, used as an independent score reference."""
    matrix = np.stack([to_dense(docs[i], dim) for i in doc_ids])
    qv = to_dense(q, dim)
    norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(qv)
    with np.errstate(divide="ignore", invalid="ignore"):
        scores = np.where(norms > 0.0, (matrix @ qv) / norms, 0.0)
    order = sorted(range(len(doc_ids)), key=lambda i: (-scores[i], doc_ids[i]))
    return [(doc_ids[i], float(scores[i])) for i in order[:k]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20260925)
    parser.add_argument("--docs", type=int, default=2000)
    parser.add_argument("--dim", type=int, default=5000)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--k", type=int, default=10)
    args = parser.parse_args()

    print(f"corpus: {args.docs} docs, dim {args.dim}, seed {args.seed}")
    docs = random_corpus(seed=args.seed, num_docs=args.docs, dim=args.dim)
    # Edge cases injected into the corpus: zero vectors and heavy negatives.
    docs[args.docs] = SparseVector([])
    docs[args.docs + 1] = SparseVector([(7, 1.0), (7, -1.0)])  # cancels to 0
    queries = random_queries(seed=args.seed + 1, num_queries=args.queries, dim=args.dim)
    queries.append(SparseVector([]))  # zero-vector query

    index = InvertedIndex()
    t0 = time.perf_counter()
    for doc_id, vec in docs.items():
        index.add(doc_id, vec)
    print(f"indexed {len(index)} docs / {index.num_dimensions} dims "
          f"in {time.perf_counter() - t0:.3f}s")

    doc_ids = sorted(docs)
    mismatches = 0
    total_candidates = 0
    t_index = t_scan = 0.0
    for qi, q in enumerate(queries):
        t0 = time.perf_counter()
        indexed = index.query(q, k=args.k)
        t_index += time.perf_counter() - t0
        t0 = time.perf_counter()
        scanned = brute_force_query(docs, q, k=args.k)
        t_scan += time.perf_counter() - t0

        total_candidates += indexed.candidates_examined
        ok = indexed.results == scanned.results
        # Independent NumPy dense cross-check on the top-k scores.
        dense = dense_topk(docs, doc_ids, q, args.dim, args.k)
        dense_ok = all(
            doc_id == d_doc and np.isclose(score, d_score, rtol=1e-9, atol=1e-12)
            for (doc_id, score), (d_doc, d_score) in zip(indexed.results, dense)
        )
        if not (ok and dense_ok):
            mismatches += 1
            print(f"  MISMATCH query {qi}: index==scan:{ok} dense-consistent:{dense_ok}")

    n = len(queries)
    print(f"queries: {n}, k={args.k}")
    print(f"avg candidates examined: {total_candidates / n:.1f} / {len(docs)} docs "
          f"({100.0 * total_candidates / n / len(docs):.1f}%)")
    print(f"avg latency: index {t_index / n * 1000:.2f} ms, "
          f"full scan {t_scan / n * 1000:.2f} ms")
    if mismatches:
        print(f"FAILED: {mismatches}/{n} queries mismatched")
        return 1
    print(f"OK: all {n} queries match full scan bitwise and dense reference")
    return 0


if __name__ == "__main__":
    sys.exit(main())
