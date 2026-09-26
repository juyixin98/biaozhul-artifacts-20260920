#!/usr/bin/env python3
"""Automated tests + benchmark for the incremental-topo JSON backend.

The C++ binary is exercised over its real line-delimited JSON interface
(stdin/stdout). Random insertions are cross-checked against an independent
naive reference (full Kahn recomputation from scratch in Python) after every
single edge, covering self-loops, a reverse-order long chain, isolated
vertices, duplicate edges, cycle-path validity, and failed-insertion
invariance.

Usage:
    python3 tests/test_cli.py            # run assertion suite + small bench
    python3 tests/test_cli.py --bench-only
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
from collections import defaultdict, deque

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "topo_server")

FAILURES = []
CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(msg)
        print(f"  FAIL: {msg}")


class Server:
    def __init__(self):
        self.proc = subprocess.Popen(
            [BIN],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read()
            raise RuntimeError(f"server closed stdout. stderr={err!r}")
        return json.loads(line)

    def close(self):
        try:
            self.send({"op": "quit"})
        except Exception:
            pass
        self.proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Naive reference implementation (full recomputation, no reuse whatsoever).
# ---------------------------------------------------------------------------

def kahn_order(n, edges):
    """edges: set of (u,v) on vertex range 0..n-1. Returns order or None."""
    indeg = [0] * n
    adj = defaultdict(list)
    for u, v in edges:
        adj[u].append(v)
        indeg[v] += 1
    q = deque(i for i in range(n) if indeg[i] == 0)
    order = []
    while q:
        x = q.popleft()
        order.append(x)
        for y in adj[x]:
            indeg[y] -= 1
            if indeg[y] == 0:
                q.append(y)
    return order if len(order) == n else None


def is_topo_order(order, edges):
    pos = {x: i for i, x in enumerate(order)}
    return all(pos[u] < pos[v] for u, v in edges)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_ping_and_limits(s):
    r = s.send({"op": "ping"})
    check(r.get("ok") is True and r.get("service") == "incremental-topo",
          "ping responds with service name")
    r = s.send({"op": "limits"})
    check(r["max_nodes"] >= 1 and r["max_edges"] >= 1, "limits are positive")


def test_self_loop(s):
    s.send({"op": "reset"})
    r = s.send({"op": "insert_edge", "u": 7, "v": 7})
    check(r["cycle"] is True and r["ok"] is False, "self-loop is a cycle")
    check(r["cycle_path"] == [7], "self-loop cycle_path is [id]")
    st = s.send({"op": "stats"})["stats"]
    check(st["nodes"] == 0 and st["edges"] == 0,
          "failed self-loop creates neither vertex nor edge")


def test_duplicate_edge(s):
    s.send({"op": "reset"})
    r1 = s.send({"op": "insert_edge", "u": 1, "v": 2})
    r2 = s.send({"op": "insert_edge", "u": 1, "v": 2})
    check(r1["ok"] and r2["ok"] and r2["duplicated"] is True,
          "duplicate edge reported and accepted")
    st = s.send({"op": "stats"})["stats"]
    check(st["edges"] == 1 and st["nodes"] == 2, "duplicate adds no edge/node")


def test_reverse_long_chain(s, n=400):
    s.send({"op": "reset"})
    for i in range(n - 1, -1, -1):
        s.send({"op": "add_node", "id": i})
    total_visited = 0
    for i in range(n - 1):
        r = s.send({"op": "insert_edge", "u": i, "v": i + 1})
        check(r["ok"] and not r["cycle"], f"chain edge {i}->{i+1} accepted")
        total_visited += r["visited"]
    order = s.send({"op": "order"})["order"]
    check(order == list(range(n)), "reverse chain ends in order 0..n-1")
    check(total_visited > 0, "reverse insertions actually visited nodes")
    ver = s.send({"op": "verify"})
    check(ver["valid"] and ver["acyclic"], "chain verifies against Kahn")

    # Closing the chain backwards must be rejected; path from 0 to n-1 exists.
    bad = s.send({"op": "insert_edge", "u": n - 1, "v": 0})
    check(bad["cycle"] and bad["cycle_path"][0] == 0
          and bad["cycle_path"][-1] == n - 1,
          "backward close reports existing path v..u")
    before = s.send({"op": "stats"})["stats"]
    s.send({"op": "insert_edge", "u": n - 1, "v": 0})
    after = s.send({"op": "stats"})["stats"]
    check(before["edges"] == after["edges"], "recycle does not add an edge")


def test_isolated_nodes(s):
    s.send({"op": "reset"})
    for x in (100, 200, 300):
        s.send({"op": "add_node", "id": x})
    s.send({"op": "insert_edge", "u": 1, "v": 2})
    order = s.send({"op": "order"})["order"]
    check(sorted(order) == [1, 2, 100, 200, 300],
          "isolated + connected vertices all present once")
    ver = s.send({"op": "verify"})
    check(ver["valid"] and ver["permutation"], "isolated nodes verify")


def test_failed_insert_invariance(s):
    s.send({"op": "reset"})
    for u, v in [(1, 2), (2, 3), (3, 4)]:
        s.send({"op": "insert_edge", "u": u, "v": v})
    before_o = s.send({"op": "order"})["order"]
    before_s = s.send({"op": "stats"})["stats"]
    r = s.send({"op": "insert_edge", "u": 4, "v": 1})
    check(r["cycle"], "4->1 closes a cycle")
    after_o = s.send({"op": "order"})["order"]
    after_s = s.send({"op": "stats"})["stats"]
    check(before_o == after_o, "failed insertion leaves order unchanged")
    check(before_s["edges"] == after_s["edges"]
          and before_s["nodes"] == after_s["nodes"],
          "failed insertion leaves graph size unchanged")


def test_cycle_path_edges_are_real(s, seed, n=40, trials=400):
    """Every reported cycle path consists of edges the graph actually has."""
    s.send({"op": "reset"})
    rng = random.Random(seed)
    edges = set()
    found = 0
    for _ in range(trials):
        u, v = rng.randrange(n), rng.randrange(n)
        r = s.send({"op": "insert_edge", "u": u, "v": v})
        if r["cycle"]:
            path = r["cycle_path"]
            check(path[0] == v and path[-1] == u,
                  "cycle path runs target v .. source u")
            if u == v:
                check(path == [u], "self-loop path singleton")
            else:
                ok_edges = all(
                    (path[i], path[i + 1]) in edges
                    for i in range(len(path) - 1)
                )
                check(ok_edges, "each cycle-path pair is a stored edge")
                check(len(path) == len(set(path)),
                      "cycle path has no repeated vertex")
                found += 1
        elif r["ok"] and not r["duplicated"]:
            edges.add((u, v))
    check(found > 0, f"seed {seed} produced at least one cycle (got {found})")


def test_random_differential(s, seed, n=80, trials=2500):
    """Compare every decision against naive Kahn recomputation."""
    s.send({"op": "reset"})
    # Pre-create vertices so the Python model and server agree on the set.
    for i in range(n):
        s.send({"op": "add_node", "id": i})

    rng = random.Random(seed)
    edges = set()
    accepted = dup = cycles = 0
    visited_sum = 0

    for t in range(trials):
        u, v = rng.randrange(n), rng.randrange(n)
        if u == v:
            would_cycle = True
        elif (u, v) in edges:
            would_cycle = False
        else:
            would_cycle = kahn_order(n, edges | {(u, v)}) is None

        r = s.send({"op": "insert_edge", "u": u, "v": v})
        visited_sum += r["visited"]

        if would_cycle:
            check(r["cycle"] and not r["ok"],
                  f"seed {seed} t={t}: expected cycle for {u}->{v}")
            cycles += 1
        else:
            check(r["ok"] and not r["cycle"],
                  f"seed {seed} t={t}: expected accept for {u}->{v}")
            if (u, v) in edges:
                check(r["duplicated"], "duplicate flagged")
                dup += 1
            else:
                edges.add((u, v))
                accepted += 1

        # Periodically cross-check the maintained order itself.
        if t % 127 == 0:
            order = s.send({"op": "order"})["order"]
            check(is_topo_order(order, edges),
                  f"seed {seed} t={t}: maintained order invalid")

    ver = s.send({"op": "verify"})
    check(ver["valid"] and ver["permutation"] and ver["acyclic"],
          f"seed {seed}: final verify valid")
    check(is_topo_order(ver["kahn_order"], edges),
          f"seed {seed}: server Kahn reference order valid")
    st = s.send({"op": "stats"})["stats"]
    check(st["edges"] == len(edges),
          f"seed {seed}: edge count matches model ({st['edges']} vs {len(edges)})")
    print(f"  differential seed={seed}: accept={accepted} dup={dup} "
          f"cycles={cycles} visited_total={visited_sum}")


def test_batch(s):
    s.send({"op": "reset"})
    r = s.send({"op": "batch", "edges": [[1, 2], [2, 3], [1, 2], [3, 1]]})
    check(r["ok"], "batch ok")
    results = r["results"]
    check(results[0]["ok"] and results[1]["ok"], "first two edges accepted")
    check(results[2]["duplicated"], "third is duplicate")
    check(results[3]["cycle"] and not results[3]["ok"], "fourth is a cycle")
    check(r["cycles"] == 1 and r["duplicates"] == 1, "batch counters correct")
    check(s.send({"op": "stats"})["stats"]["edges"] == 2,
          "batch leaves exactly the acyclic edges")


def test_malformed_input(s):
    cases = [
        ('{"op":', "invalid JSON rejected"),
        ('{"op":"no_such_op"}', "unknown op rejected"),
        ('{"op":"insert_edge","u":"x","v":2}', "non-integer endpoint rejected"),
        ('[1,2,3]', "non-object request rejected"),
    ]
    # Use a raw channel once: Server.send writes JSON, so send bad text via proc.
    for raw, msg in cases:
        s.proc.stdin.write(raw + "\n")
        s.proc.stdin.flush()
        r = json.loads(s.proc.stdout.readline())
        check(r.get("ok") is False and "error" in r, msg)


# ---------------------------------------------------------------------------
# Benchmark (evidence of actual visited-node counts, bounded scale).
# ---------------------------------------------------------------------------

def benchmark(s):
    print("\n== benchmark (bounded scale) ==")
    rows = []

    # A: sparse random forward edges over a reversed vertex layout.
    for n, m in [(2000, 8000), (20000, 100000)]:
        s.send({"op": "reset"})
        for i in range(n - 1, -1, -1):
            s.send({"op": "add_node", "id": i})
        rng = random.Random(2026)
        pairs = set()
        while len(pairs) < m:
            a, b = rng.randrange(n), rng.randrange(n)
            if a < b:  # always acyclic
                pairs.add((a, b))
        t0 = time.time()
        r = s.send({"op": "batch", "edges": [list(p) for p in pairs]})
        dt = time.time() - t0
        st = s.send({"op": "stats"})["stats"]
        check(st["edges"] == m, f"bench A n={n}: all edges stored")
        check(s.send({"op": "verify"})["valid"], f"bench A n={n}: order valid")
        rows.append((f"random-forward n={n} m={m}", m, st["reorders"],
                     st["total_visited"],
                     f"{st['total_visited']/m:.2f}", f"{dt:.2f}s"))

    # B: reverse-order long chain (every edge forces a reorder).
    n = 5000
    s.send({"op": "reset"})
    for i in range(n - 1, -1, -1):
        s.send({"op": "add_node", "id": i})
    t0 = time.time()
    s.send({"op": "batch", "edges": [[i, i + 1] for i in range(n - 1)]})
    dt = time.time() - t0
    st = s.send({"op": "stats"})["stats"]
    check(s.send({"op": "verify"})["valid"], "bench chain: order valid")
    rows.append((f"reverse-chain n={n}", n - 1, st["reorders"],
                 st["total_visited"],
                 f"{st['total_visited']/(n-1):.2f}", f"{dt:.2f}s"))

    print(f"{'scenario':28} {'edges':>7} {'reorders':>9} "
          f"{'visited(total)':>15} {'visited/edge':>13} {'time':>7}")
    for row in rows:
        print(f"{row[0]:28} {row[1]:>7} {row[2]:>9} {row[3]:>15} "
              f"{row[4]:>13} {row[5]:>7}")


def main():
    if not os.path.exists(BIN):
        print(f"binary not found at {BIN}; run `make` first", file=sys.stderr)
        return 2

    s = Server()
    try:
        if not ARGS.bench_only:
            test_ping_and_limits(s)
            test_self_loop(s)
            test_duplicate_edge(s)
            test_reverse_long_chain(s)
            test_isolated_nodes(s)
            test_failed_insert_invariance(s)
            test_batch(s)
            test_malformed_input(s)
            test_cycle_path_edges_are_real(s, seed=11)
            test_cycle_path_edges_are_real(s, seed=22, n=70, trials=700)
            test_random_differential(s, seed=101)
            test_random_differential(s, seed=202, n=120, trials=4000)
            test_random_differential(s, seed=303, n=40, trials=1500)
        benchmark(s)
    finally:
        s.close()

    print(f"\nchecks={CHECKS} failures={len(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench-only", action="store_true")
    ARGS = ap.parse_args()
    sys.exit(main())
