#!/usr/bin/env python3
"""End-to-end tests for the topo_srv JSON interface.

Runs the compiled server as a subprocess, feeds newline-delimited JSON
requests, and cross-checks every accepted stream with an *independent*
Kahn implementation written in Python. Covers self loops, a reverse-order
long chain, isolated vertices, duplicates, cycle witnesses, failed-insert
atomicity, randomized differential streams, and malformed/protocol inputs.

Exit code 0 iff all tests pass.
"""

import json
import os
import random
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRV = os.path.join(ROOT, "build", "topo_srv")

failures = []
checks = 0


def check(cond, label):
    global checks
    checks += 1
    if not cond:
        failures.append(label)
        print(f"  FAIL: {label}")


class Server:
    def __init__(self):
        self.p = subprocess.Popen(
            [SRV], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )

    def send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        return json.loads(line)

    def send_raw(self, text):
        self.p.stdin.write(text + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def close(self):
        try:
            self.p.stdin.close()
        except BrokenPipeError:
            pass
        self.p.wait(timeout=5)


def kahn(n, edges):
    """Independent Python Kahn reference over a set of (u,v) edges."""
    out = [[] for _ in range(n)]
    indeg = [0] * n
    for u, v in edges:
        out[u].append(v)
        indeg[v] += 1
    queue = [i for i in range(n) if indeg[i] == 0]
    order = []
    while queue:
        u = queue.pop()
        order.append(u)
        for v in out[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    return order if len(order) == n else None


def has_path(n, edges, s, t):
    out = {}
    for u, v in edges:
        out.setdefault(u, []).append(v)
    seen = {s}
    stack = [s]
    while stack:
        x = stack.pop()
        for y in out.get(x, []):
            if y == t:
                return True
            if y not in seen:
                seen.add(y)
                stack.append(y)
    return False


def is_topological(n, edges, order):
    if sorted(order) != list(range(n)):
        return False
    pos = {v: i for i, v in enumerate(order)}
    return all(pos[u] < pos[v] for u, v in edges)


def test_basic_and_protocol():
    print("[e2e] protocol, self-loop, cycle, duplicates, isolated vertices")
    s = Server()
    r = s.send({"op": "order"})
    check(r["ok"] is False and r["error"] == "no_graph", "order before reset rejected")

    n = 8
    r = s.send({"op": "reset", "n": n})
    check(r["ok"] and r["n"] == n, "reset ok")
    r = s.send({"op": "reset", "n": -1})
    check(r["ok"] is False, "negative n rejected")
    r = s.send({"op": "addEdge", "u": 0, "v": 99})
    check(r["ok"] is False and r["error"] == "out_of_range", "vertex id bounds checked")

    edges = set()
    for u, v in [(2, 6), (6, 4), (4, 1), (7, 0)]:
        r = s.send({"op": "addEdge", "u": u, "v": v})
        check(r["ok"] and r["accepted"], f"edge {u}->{v} accepted")
        edges.add((u, v))

    r = s.send({"op": "order"})
    check(is_topological(n, edges, r["order"]), "order is topological; isolated kept")

    # self loop
    before = s.send({"op": "order"})["order"]
    r = s.send({"op": "addEdge", "u": 3, "v": 3})
    check(r["cycle"] and not r["accepted"] and r["cyclePath"] == [3, 3],
          "self-loop rejected with [u,u]")
    check(r["graphUnchanged"] is True, "self-loop reports graph unchanged")
    check(s.send({"op": "order"})["order"] == before, "order unchanged by self-loop")

    # cycle close: 0 already has 7->0; add 0->7
    r = s.send({"op": "addEdge", "u": 0, "v": 7})
    check(r["cycle"] and r["cyclePath"] == [0, 7, 0], "two-node cycle witness")
    check(r["graphUnchanged"] and not s.send({"op": "hasEdge", "u": 0, "v": 7})["hasEdge"],
          "failed cycle insert stored no edge")

    # duplicates do no work (use a fresh, backwards edge so the first call
    # genuinely inserts and reorders)
    r1 = s.send({"op": "addEdge", "u": 1, "v": 5})
    r2 = s.send({"op": "addEdge", "u": 1, "v": 5})
    check(r1["duplicate"] is False and r2["duplicate"] is True
          and r2["visited"] == 0 and r2["reordered"] is False,
          "duplicate idempotent with zero search work")

    r = s.send({"op": "validate"})
    check(r["incrementalOrderValid"] and r["referenceAcyclic"],
          "server-side independent validation passes")

    # malformed input never kills the session
    r = s.send_raw("not json")
    check(r["ok"] is False and r["error"] == "bad_json", "malformed JSON reported")
    r = s.send({"op": "unknown"})
    check(r["ok"] is False and r["error"] == "unknown_op", "unknown op reported")
    r = s.send({"op": "order"})
    check(r["ok"], "session survives bad input")

    # batched ops
    r = s.send({"ops": [
        {"op": "reset", "n": 4},
        {"op": "addEdge", "u": 0, "v": 1},
        {"op": "addEdge", "u": 1, "v": 2},
        {"op": "order"},
    ]})
    check(r["ok"] and len(r["results"]) == 4, "batch executed")
    check(r["results"][3]["order"][:3] == [0, 1, 2], "batch order correct")

    s.close()


def test_reverse_chain_via_server():
    print("[e2e] reverse-order long chain through JSON protocol (n=1000)")
    s = Server()
    n = 1000
    s.send({"op": "reset", "n": n})
    total_visited = 0
    for i in range(n - 1, 0, -1):
        r = s.send({"op": "addEdge", "u": i, "v": i - 1})
        check(r["accepted"] and r["reordered"], "reverse chain edge reordered")
        total_visited += r["visited"]
    r = s.send({"op": "order"})
    check(r["order"] == list(range(n - 1, -1, -1)), "reverse chain final order")
    print(f"           total visited={total_visited}"
          f" avg/insert={total_visited / (n - 1):.2f}")
    r = s.send({"op": "addEdge", "u": 0, "v": n - 1})
    check(r["cycle"] and r["cyclePath"][0] == 0 and r["cyclePath"][-1] == 0
          and len(r["cyclePath"]) == n + 1,
          "closing the reversed chain reports the full cycle")
    s.close()


def test_random_streams():
    print("[e2e] randomized streams vs independent Python Kahn")
    rng = random.Random(20260925)
    grand_visited = 0
    grand_reordered = 0
    for trial in range(12):
        n = rng.randint(1, 60)
        s = Server()
        s.send({"op": "reset", "n": n})
        edges = set()
        attempts = n * n
        for _ in range(attempts):
            u, v = rng.randrange(n), rng.randrange(n)
            r = s.send({"op": "addEdge", "u": u, "v": v})
            if (u, v) in edges or u == v:
                if u == v:
                    check(r["cycle"] and not r["accepted"], "self loop via stream")
                else:
                    check(r["duplicate"], "duplicate via stream")
                continue
            if kahn(n, edges | {(u, v)}) is None:
                check(r["cycle"] and not r["accepted"],
                      "cycle decision matches Python Kahn")
                check(r["graphUnchanged"] is True, "rejection atomic")
                if u != v:
                    path = r["cyclePath"]
                    check(path[0] == u and path[-1] == u and path[1] == v,
                          "witness endpoints correct")
                    check(all((path[i], path[i + 1]) in edges
                              for i in range(1, len(path) - 1)),
                          "witness interior uses real edges")
                    check(has_path(n, edges, v, u),
                          "witness corresponds to an actual v->*u route")
            else:
                check(r["accepted"] and not r["cycle"], "accept matches Kahn")
                edges.add((u, v))
                if r["reordered"]:
                    grand_visited += r["visited"]
                    grand_reordered += 1
        order = s.send({"op": "order"})["order"]
        check(is_topological(n, edges, order),
              "final server order independently verified topological")
        s.close()
    print(f"           reordered inserts={grand_reordered} "
          f"total visited={grand_visited} "
          f"avg={grand_visited / grand_reordered:.2f}")


def main():
    if not os.path.exists(SRV):
        print(f"server binary not found at {SRV}; run `make` first", file=sys.stderr)
        return 2
    test_basic_and_protocol()
    test_reverse_chain_via_server()
    test_random_streams()
    print(f"\nchecks={checks} failures={len(failures)}")
    if failures:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
