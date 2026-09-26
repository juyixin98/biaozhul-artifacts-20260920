#!/usr/bin/env python3
"""Generate the 100k-vertex / 1M-edge scale payload used for the manual
benchmark in README. Writes examples/scale_100k.json (~23 MB).

Structure: 50,000 disjoint 2-cycles (2i <-> 2i+1) linked into a chain of
SCCs, plus heavy parallel copies of edge 0->1 to reach exactly 1,000,000
raw edges. Covers self-contained cycles, cross-SCC edges and multi-edges.
"""
import json
import os
import random
import sys

N = 100_000
TARGET_RAW_EDGES = 1_000_000


def main():
    random.seed(42)
    edges = []
    for i in range(0, N, 2):
        edges.append((i, i + 1))
        edges.append((i + 1, i))
    for i in range(0, N - 2, 2):
        edges.append((i + 1, i + 2))
    while len(edges) < TARGET_RAW_EDGES:
        edges.append((0, 1))
    random.shuffle(edges)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scale_100k.json")
    with open(out_path, "w") as f:
        json.dump({"vertices": N, "edges": [{"from": u, "to": v} for u, v in edges]}, f)
    print(f"wrote {out_path} with {len(edges)} raw edges", file=sys.stderr)


if __name__ == "__main__":
    main()
