#!/usr/bin/env python3
"""Randomized cross-validation of the three operations.

For random DAGs (n <= 9, so full enumeration is cheap):
  1. enumerate returns every path, in lexicographic order, without duplicates;
  2. count == number of enumerated paths;
  3. reverse ranking: kth(i+1) == paths[i] for sampled ranks;
  4. kth(0) and kth(len+1) are rejected with K_OUT_OF_RANGE;
  5. every enumerated path is a valid s-t path (edges exist, nodes distinct).

Fixed seed => reproducible. Exits non-zero on any mismatch.
"""
import json
import random
import subprocess
import sys

BIN = "./dagpaths"
SEED = 20260925
INSTANCES = 300

failures = []


def call(request):
    proc = subprocess.run([BIN], input=json.dumps(request),
                          capture_output=True, text=True)
    return json.loads(proc.stdout)


def check(cond, msg):
    if not cond:
        failures.append(msg)


def validate_instance(rng, n, edges, s, t):
    graph = {"num_nodes": n, "edges": edges}
    tag = f"n={n} edges={edges} s={s} t={t}"

    enum = call({"op": "enumerate", "graph": graph, "source": s, "target": t,
                 "max_paths": 100000})
    cnt = call({"op": "count", "graph": graph, "source": s, "target": t})
    check(enum["ok"] and cnt["ok"], f"ok flags failed: {tag}")
    if not (enum["ok"] and cnt["ok"]):
        return

    paths = enum["paths"]
    check(not enum["truncated"], f"unexpected truncation: {tag}")
    check(int(cnt["count"]) == len(paths),
          f"count {cnt['count']} != enumerated {len(paths)}: {tag}")
    check(paths == sorted(paths), f"enumerate not in lex order: {tag}")
    check(len(set(map(tuple, paths))) == len(paths), f"duplicate paths: {tag}")

    edge_set = set(map(tuple, edges))
    for p in paths:
        valid = (p[0] == s and p[-1] == t and len(set(p)) == len(p)
                 and all((a, b) in edge_set for a, b in zip(p, p[1:])))
        check(valid, f"invalid path {p}: {tag}")

    # Reverse ranking: every path (or a sample) must round-trip through kth.
    ranks = range(1, len(paths) + 1)
    if len(paths) > 25:
        ranks = sorted(rng.sample(range(1, len(paths) + 1), 25))
    for i in ranks:
        resp = call({"op": "kth", "graph": graph, "source": s, "target": t, "k": str(i)})
        check(resp["ok"] and resp["path"] == paths[i - 1],
              f"kth({i}) mismatch: {tag} got {resp.get('path')}")

    for bad_k in ("0", str(len(paths) + 1)):
        resp = call({"op": "kth", "graph": graph, "source": s, "target": t, "k": bad_k})
        check(not resp["ok"] and resp["error"]["code"] == "K_OUT_OF_RANGE",
              f"kth({bad_k}) not rejected: {tag}")


def main():
    rng = random.Random(SEED)
    for _ in range(INSTANCES):
        n = rng.randint(1, 9)
        p = rng.choice([0.2, 0.4, 0.6])
        edges = [[i, j] for i in range(n) for j in range(i + 1, n)
                 if rng.random() < p]
        s = rng.randrange(n)
        t = rng.randrange(n)
        validate_instance(rng, n, edges, s, t)
    if failures:
        print(f"random_check: {len(failures)} failure(s):", file=sys.stderr)
        for f in failures[:20]:
            print(f"  {f}", file=sys.stderr)
        return 1
    print(f"random_check: {INSTANCES} random DAGs passed "
          f"(count == enumerate == reverse kth ranking)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
