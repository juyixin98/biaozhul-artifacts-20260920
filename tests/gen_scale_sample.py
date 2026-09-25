#!/usr/bin/env python3
"""Generates tests/requests/scale_1000.json: a 1000-node structured CFG
(chain of diamonds with periodic loop back edges) plus one unreachable
cycle, used as scale/performance evidence. Deterministic output."""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "requests", "scale_1000.json")

N = 1000
nodes = [f"b{i}" for i in range(N)] + ["u0", "u1"]
edges = []

# Main chain: b_i -> b_{i+1}
for i in range(N - 1):
    edges.append([f"b{i}", f"b{i+1}"])

# Every 10th block branches to a side block that rejoins two ahead
# (a diamond), creating non-trivial dominance frontiers.
for i in range(0, N - 2, 10):
    edges.append([f"b{i}", f"b{i+2}"])

# Every 50th block jumps back 25 blocks (loop back edge).
for i in range(50, N, 50):
    edges.append([f"b{i}", f"b{i-25}"])

# Unreachable cycle, deliberately disconnected from the entry.
edges.append(["u0", "u1"])
edges.append(["u1", "u0"])

req = {
    "entry": "b0",
    "nodes": nodes,
    "edges": edges,
    "queries": [
        {"type": "idom", "node": "b999"},
        {"type": "frontier", "node": "b500"},
        {"type": "dom_chain", "node": "b999"},
        {"type": "dominates", "a": "b0", "b": "b999"},
        {"type": "dominates", "a": "b100", "b": "b50"},
        {"type": "idom", "node": "u0"},
    ],
}

with open(OUT, "w") as f:
    json.dump(req, f, indent=2)
    f.write("\n")
print(f"wrote {OUT}: {len(nodes)} nodes, {len(edges)} edges")
