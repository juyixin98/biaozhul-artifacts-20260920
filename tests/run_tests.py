#!/usr/bin/env python3
"""Automated tests for the offline dynamic-connectivity backend.

Three independent implementations are compared on every random workload:

  1. segment-tree + rollback DSU  (the C++ "segment-tree" algorithm)
  2. per-query adjacency rebuild + BFS (the C++ "naive-bfs" reference)
  3. an independent Python BFS oracle written for this test harness

Fixed cases additionally cover parallel edges, duplicate deletes, unknown
edge ids, query-time boundaries, scale limits, malformed input and stats.

All commands, stdout/stderr and timings are appended to tests/test_results.log.
Exit code is non-zero if any check fails.
"""

import json
import math
import os
import random
import subprocess
import sys
import time
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.environ.get("DC_BINARY", os.path.join(ROOT, "build", "dynamic_connectivity"))
LOG_PATH = os.path.join(ROOT, "tests", "test_results.log")

PASSED = []
FAILED = []
LOG_FH = None


def log(line=""):
    print(line)
    LOG_FH.write(line + "\n")
    LOG_FH.flush()


def run_backend(request, algorithm=None, expect_rc=None):
    """Invokes the binary with one JSON request; returns (rc, parsed_or_none)."""
    payload = dict(request)
    if algorithm is not None:
        payload["algorithm"] = algorithm
    body = json.dumps(payload)
    cmd = [BIN]
    started = time.perf_counter()
    proc = subprocess.run(
        cmd, input=body, capture_output=True, text=True, timeout=120
    )
    elapsed = time.perf_counter() - started
    LOG_FH.write(f"$ echo '<json {len(body)} bytes>' | {' '.join(cmd)}"
                 f"  # algorithm={algorithm} rc={proc.returncode} {elapsed*1000:.1f} ms\n")
    if proc.stderr.strip():
        LOG_FH.write("  stderr: " + proc.stderr.strip() + "\n")
    LOG_FH.flush()
    if expect_rc is not None:
        check("return code", proc.returncode, expect_rc)
    parsed = None
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            check(f"valid JSON output for algorithm={algorithm}", False, True)
            return proc.returncode, None, elapsed
    return proc.returncode, parsed, elapsed


def check(name, actual, expected):
    if actual == expected:
        PASSED.append(name)
        return True
    FAILED.append((name, actual, expected))
    log(f"  FAIL: {name}: actual={actual!r} expected={expected!r}")
    return False


def check_true(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        return True
    FAILED.append((name, False, True))
    log(f"  FAIL: {name} {detail}")
    return False


# ---------------------------------------------------------------------------
# Independent Python oracle
# ---------------------------------------------------------------------------


def python_oracle(n, ops):
    """Replays the op stream online and BFSes at every query.

    Returns a list of booleans in query order. Assumes a valid stream whose
    delete edge_ids refer to currently active instances.
    """
    active = {}  # instance id -> (u, v); deletions match exact instances
    next_id = 0
    answers = []
    for op in ops:
        t = op["type"]
        if t == "add":
            active[next_id] = (op["u"], op["v"])
            next_id += 1
        elif t == "delete":
            active.pop(op["edge_id"], None)
        else:
            adj = [[] for _ in range(n)]
            for u, v in active.values():
                adj[u].append(v)
                adj[v].append(u)
            seen = [False] * n
            q = deque([op["u"]])
            seen[op["u"]] = True
            while q:
                x = q.popleft()
                for y in adj[x]:
                    if not seen[y]:
                        seen[y] = True
                        q.append(y)
            answers.append(seen[op["v"]])
    return answers


def connected_flags(resp):
    return [r["connected"] for r in resp["results"]]


# ---------------------------------------------------------------------------
# Random differential testing
# ---------------------------------------------------------------------------


def generate_valid_stream(rng, n, length):
    """Generates a guaranteed-valid op stream (deletes hit active instances)."""
    ops = []
    active_ids = []
    next_id = 0
    for _ in range(length):
        choices = ["add", "query"]
        weights = [3, 4]
        if active_ids:
            choices.append("delete")
            weights.append(2)
        kind = rng.choices(choices, weights=weights, k=1)[0]
        if kind == "add":
            u = rng.randrange(n)
            v = rng.randrange(n)  # may equal u: self loops are legal
            ops.append({"type": "add", "u": u, "v": v})
            active_ids.append(next_id)
            next_id += 1
        elif kind == "delete":
            idx = rng.randrange(len(active_ids))
            eid = active_ids.pop(idx)
            ops.append({"type": "delete", "edge_id": eid})
        else:
            u = rng.randrange(n)
            v = rng.randrange(n)
            ops.append({"type": "query", "u": u, "v": v})
    return ops


def random_differential_tests():
    log("== random differential tests (segment-tree vs naive-bfs vs python BFS) ==")
    configs = [
        (1, 30, 101),
        (2, 60, 102),
        (3, 120, 103),
        (5, 200, 201),
        (10, 400, 202),
        (50, 800, 303),
        (200, 1500, 404),
    ]
    for n, length, seed in configs:
        rng = random.Random(seed)
        for rep in range(5):
            ops = generate_valid_stream(rng, n, length)
            request = {"n": n, "operations": ops}
            expected = python_oracle(n, ops)
            _, seg, _ = run_backend(request, "segment-tree")
            _, nai, _ = run_backend(request, "naive-bfs")
            tag = f"n={n},len={length},seed={seed},rep={rep}"
            if seg is None or nai is None:
                check_true(f"both algorithms produced output [{tag}]", False)
                continue
            check_true(f"segment-tree ok [{tag}]", seg.get("ok") is True, str(seg.get("error")))
            check_true(f"naive-bfs ok [{tag}]", nai.get("ok") is True, str(nai.get("error")))
            seg_flags = connected_flags(seg)
            nai_flags = connected_flags(nai)
            check(f"segment-tree == python BFS [{tag}]", seg_flags, expected)
            check(f"naive-bfs == python BFS [{tag}]", nai_flags, expected)
            # Every queried pair must be reported in op order.
            check(f"result count == query count [{tag}]",
                  len(seg_flags), expected.__len__())


# ---------------------------------------------------------------------------
# Fixed functional cases
# ---------------------------------------------------------------------------


def fixed_cases():
    log("== fixed functional cases ==")

    # Parallel edges: deleting one instance must leave the other active.
    req = {"n": 2, "operations": [
        {"type": "add", "u": 0, "v": 1},
        {"type": "add", "u": 0, "v": 1},
        {"type": "query", "u": 0, "v": 1},
        {"type": "delete", "edge_id": 0},
        {"type": "query", "u": 0, "v": 1},
        {"type": "delete", "edge_id": 1},
        {"type": "query", "u": 0, "v": 1},
    ]}
    expected = python_oracle(2, req["operations"])
    check("parallel-edge python oracle", expected, [True, True, False])
    _, resp, _ = run_backend(req, "segment-tree", expect_rc=0)
    check("parallel edges: flags", connected_flags(resp), [True, True, False])
    check("parallel edges: add instance ids", resp["edge_ids"], [0, 1])

    # Duplicate delete of an already removed instance is a hard error.
    req = {"n": 2, "operations": [
        {"type": "add", "u": 0, "v": 1},
        {"type": "delete", "edge_id": 0},
        {"type": "delete", "edge_id": 0},
    ]}
    rc, resp, _ = run_backend(req, "segment-tree", expect_rc=2)
    check("duplicate delete rejected", resp["ok"], False)
    check("duplicate delete code", resp["error"]["code"], "duplicate_delete")
    check("duplicate delete op_index", resp["error"]["op_index"], 2)
    rc, resp, _ = run_backend(req, "naive-bfs", expect_rc=2)
    check("duplicate delete rejected (naive too)", resp["error"]["code"], "duplicate_delete")

    # Delete of an id no add ever returned.
    req = {"n": 2, "operations": [{"type": "delete", "edge_id": 7}]}
    rc, resp, _ = run_backend(req, "segment-tree", expect_rc=2)
    check("unknown edge id code", resp["error"]["code"], "unknown_edge_id")

    # Negative edge id.
    req = {"n": 2, "operations": [{"type": "delete", "edge_id": -1}]}
    rc, resp, _ = run_backend(req, "segment-tree", expect_rc=2)
    check("negative edge id code", resp["error"]["code"], "invalid_operation")

    # Boundary: query as the very first op, before any edge exists.
    req = {"n": 3, "operations": [
        {"type": "query", "u": 0, "v": 1},
        {"type": "add", "u": 0, "v": 1},
        {"type": "query", "u": 0, "v": 1},
        {"type": "query", "u": 0, "v": 2},
    ]}
    expected = python_oracle(3, req["operations"])
    check("early-query oracle", expected, [False, True, False])
    _, resp, _ = run_backend(req, "segment-tree", expect_rc=0)
    check("query-before-any-add boundary", connected_flags(resp), [False, True, False])
    # Default t labels equal op indices.
    check("default t labels", [r["t"] for r in resp["results"]], [0, 2, 3])
    check("op_index labels", [r["op_index"] for r in resp["results"]], [0, 2, 3])

    # Boundary: add then immediately delete, then query — lifetime never
    # overlaps a query, interval is empty.
    req = {"n": 2, "operations": [
        {"type": "add", "u": 0, "v": 1},
        {"type": "delete", "edge_id": 0},
        {"type": "query", "u": 0, "v": 1},
    ]}
    _, resp, _ = run_backend(req, "segment-tree", expect_rc=0)
    check("edge gone before first query", connected_flags(resp), [False])
    check_true("empty interval yields zero segment placements",
               resp["stats"]["segment_placements"] == 0,
               str(resp["stats"]))

    # Boundary: query is the final op and edge is never deleted.
    req = {"n": 4, "operations": [
        {"type": "add", "u": 1, "v": 3},
        {"type": "query", "u": 3, "v": 1},
        {"type": "query", "u": 0, "v": 2},
    ]}
    _, resp, _ = run_backend(req, "segment-tree", expect_rc=0)
    check("never-deleted edge at final query", connected_flags(resp), [True, False])

    # Self loop: u == v is connected even for an isolated vertex.
    req = {"n": 3, "operations": [
        {"type": "query", "u": 2, "v": 2},
        {"type": "add", "u": 0, "v": 0},
        {"type": "query", "u": 0, "v": 0},
    ]}
    expected = python_oracle(3, req["operations"])
    _, resp, _ = run_backend(req, "segment-tree", expect_rc=0)
    check("self-loop semantics", connected_flags(resp), expected)

    # Empty operation list.
    req = {"n": 1, "operations": []}
    _, resp, _ = run_backend(req, "segment-tree", expect_rc=0)
    check("empty ops ok", resp["ok"], True)
    check("empty ops results", resp["results"], [])

    # No queries at all.
    req = {"n": 2, "operations": [
        {"type": "add", "u": 0, "v": 1},
        {"type": "delete", "edge_id": 0},
    ]}
    _, resp, _ = run_backend(req, "segment-tree", expect_rc=0)
    check("no queries ok", resp["ok"], True)
    check("no queries results", resp["results"], [])

    # Explicit t labels are echoed verbatim.
    req = {"n": 2, "operations": [
        {"type": "add", "u": 0, "v": 1},
        {"type": "query", "t": 100, "u": 0, "v": 1},
    ]}
    _, resp, _ = run_backend(req, "segment-tree", expect_rc=0)
    check("explicit t echoed", resp["results"][0]["t"], 100)

    # Vertex out of range in add / query.
    for bad_op in ({"type": "add", "u": 0, "v": 3},
                   {"type": "query", "u": 0, "v": 3}):
        req = {"n": 3, "operations": [bad_op]}
        rc, resp, _ = run_backend(req, "segment-tree", expect_rc=2)
        check(f"vertex out of range rejected: {bad_op['type']}",
              resp["error"]["code"], "vertex_out_of_range")

    # Undirected symmetry across a two-component merge/split cycle.
    req = {"n": 5, "operations": [
        {"type": "add", "u": 0, "v": 1},
        {"type": "add", "u": 2, "v": 3},
        {"type": "query", "u": 0, "v": 3},
        {"type": "add", "u": 1, "v": 2},
        {"type": "query", "u": 0, "v": 3},
        {"type": "delete", "edge_id": 2},
        {"type": "query", "u": 0, "v": 3},
        {"type": "query", "u": 2, "v": 3},
    ]}
    expected = python_oracle(5, req["operations"])
    _, seg, _ = run_backend(req, "segment-tree", expect_rc=0)
    _, nai, _ = run_backend(req, "naive-bfs", expect_rc=0)
    check("merge/split cycle oracle", expected, [False, True, False, True])
    check("merge/split cycle segment-tree", connected_flags(seg), expected)
    check("merge/split cycle naive-bfs", connected_flags(nai), expected)


def validation_cases():
    log("== input validation / scale-limit cases ==")

    def raw_body(body, expect_rc=1):
        cmd = [BIN]
        proc = subprocess.run(cmd, input=body, capture_output=True, text=True, timeout=30)
        LOG_FH.write(f"$ echo '<raw {len(body)} bytes>' | {BIN}  # rc={proc.returncode}\n")
        LOG_FH.flush()
        check(f"raw rc for: {body[:40]!r}", proc.returncode, expect_rc)
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            check_true("raw output is JSON", False)
            return None

    resp = raw_body("{not json")
    check("malformed json code", resp["error"]["code"], "invalid_json")

    resp = raw_body("[]")
    check("non-object body code", resp["error"]["code"], "invalid_request")

    resp = raw_body('{"n": 1' + "0" * 100 + ', "operations": []}', expect_rc=1)
    check("huge integer literal rejected as JSON", resp["error"]["code"], "invalid_json")

    for bad_n in (0, -1, 100001):
        req = json.dumps({"n": bad_n, "operations": []})
        resp = raw_body(req, expect_rc=2)
        check(f"n={bad_n} rejected", resp["error"]["code"], "invalid_n")

    req = json.dumps({"n": 2, "operations": [{"type": "frobnicate"}]})
    resp = raw_body(req, expect_rc=2)
    check("unknown op type", resp["error"]["code"], "invalid_operation")
    check("unknown op type op_index", resp["error"]["op_index"], 0)

    req = json.dumps({"n": 2, "operations": "nope"})
    resp = raw_body(req, expect_rc=2)
    check("operations not array", resp["error"]["code"], "invalid_operations")

    # Too many operations.
    req = {"n": 2, "operations": [{"type": "query", "u": 0, "v": 0}] * 200001}
    rc, resp, _ = run_backend(req, "segment-tree", expect_rc=2)
    check("200001 ops rejected", resp["error"]["code"], "too_many_operations")


# ---------------------------------------------------------------------------
# Stats sanity
# ---------------------------------------------------------------------------


def stats_cases():
    log("== stats sanity ==")
    rng = random.Random(909)
    n, length = 64, 1000
    ops = generate_valid_stream(rng, n, length)
    req = {"n": n, "operations": ops}
    _, resp, _ = run_backend(req, "segment-tree", expect_rc=0)
    stats = resp["stats"]
    num_adds = sum(1 for o in ops if o["type"] == "add")
    num_dels = sum(1 for o in ops if o["type"] == "delete")
    num_qs = sum(1 for o in ops if o["type"] == "query")
    check("stats num_adds", stats["num_adds"], num_adds)
    check("stats num_deletes", stats["num_deletes"], num_dels)
    check("stats num_queries", stats["num_queries"], num_qs)
    check_true("merge_calls <= union_calls",
               stats["merge_calls"] <= stats["union_calls"], str(stats))
    # Every interval touches at most O(log Q) segment-tree nodes.
    bound = max(1, 2 * max(1, num_qs).bit_length())
    check_true("placements <= adds * 2*ceil(log2 Q)",
               stats["segment_placements"] <= num_adds * bound,
               f"{stats['segment_placements']} vs {num_adds * bound}")
    check_true("rollback stack bounded by adds",
               stats["max_rollback_stack"] <= num_adds, str(stats))


# ---------------------------------------------------------------------------
# Performance: bounded-scale evidence, no solver libraries used
# ---------------------------------------------------------------------------


def generate_mixed(rng, n, length):
    """Dense churn workload: adds, deletions of active instances, queries."""
    ops = []
    active = []
    next_id = 0
    for i in range(length):
        if i % 5 == 4 or (active and rng.random() < 0.25):
            if active:
                idx = rng.randrange(len(active))
                ops.append({"type": "delete", "edge_id": active.pop(idx)})
                continue
        roll = rng.random()
        if roll < 0.45:
            u, v = rng.randrange(n), rng.randrange(n)
            ops.append({"type": "add", "u": u, "v": v})
            active.append(next_id)
            next_id += 1
        else:
            u, v = rng.randrange(n), rng.randrange(n)
            ops.append({"type": "query", "u": u, "v": v})
    return ops


def performance_cases():
    log("== performance / scale cases ==")

    # Moderate workload where the naive reference is still affordable; this
    # gives direct timing evidence of the asymptotic gap.
    rng = random.Random(77)
    n_mod, len_mod = 300, 3000
    ops_mod = generate_mixed(rng, n_mod, len_mod)
    req_mod = {"n": n_mod, "operations": ops_mod}
    _, seg_mod, t_seg_mod = run_backend(req_mod, "segment-tree", expect_rc=0)
    _, nai_mod, t_nai_mod = run_backend(req_mod, "naive-bfs", expect_rc=0)
    check("moderate: answers agree",
          connected_flags(seg_mod), connected_flags(nai_mod))
    log(f"  moderate n={n_mod}, ops={len_mod}: "
        f"segment-tree {t_seg_mod*1000:.1f} ms vs naive-bfs {t_nai_mod*1000:.1f} ms")

    # Dense long-lived edges followed by many queries: naive rebuilds the whole
    # adjacency list for every query (O(Q*E)), while the segment tree stores
    # each edge on O(log Q) nodes and unions it once per node.
    rng2 = random.Random(78)
    n_dense, e_dense, q_dense = 2000, 1500, 5000
    dense_ops = [{"type": "add", "u": rng2.randrange(n_dense),
                  "v": rng2.randrange(n_dense)} for _ in range(e_dense)]
    dense_ops += [{"type": "query", "u": rng2.randrange(n_dense),
                   "v": rng2.randrange(n_dense)} for _ in range(q_dense)]
    req_dense = {"n": n_dense, "operations": dense_ops}
    _, seg_dense, t_seg_dense = run_backend(req_dense, "segment-tree", expect_rc=0)
    _, nai_dense, t_nai_dense = run_backend(req_dense, "naive-bfs", expect_rc=0)
    check("dense: answers agree",
          connected_flags(seg_dense), connected_flags(nai_dense))
    log(f"  dense n={n_dense}, E={e_dense}, Q={q_dense}: "
        f"segment-tree {t_seg_dense*1000:.1f} ms vs naive-bfs {t_nai_dense*1000:.1f} ms")
    check_true("dense: segment-tree beats naive-bfs", t_seg_dense < t_nai_dense,
               f"{t_seg_dense*1000:.1f} vs {t_nai_dense*1000:.1f} ms")

    # Bounded max-ish workload for the segment-tree solver.
    rng = random.Random(88)
    n_big, len_big = 20000, 120000
    ops_big = generate_mixed(rng, n_big, len_big)
    req_big = {"n": n_big, "operations": ops_big}
    body = json.dumps(req_big)
    log(f"  large request size: {len(body)/1024:.1f} KiB")
    _, seg_big, t_big = run_backend(req_big, "segment-tree", expect_rc=0)
    expected_queries = sum(1 for o in ops_big if o["type"] == "query")
    check("large: query count", len(seg_big["results"]), expected_queries)
    log(f"  LARGE n={n_big}, ops={len_big}, queries={expected_queries}: "
        f"segment-tree {t_big*1000:.1f} ms")
    log(f"  LARGE stats: {json.dumps(seg_big['stats'])}")
    check_true("large run under 15 s", t_big < 15.0, f"{t_big:.2f} s")

    # Spot-check answers on a prefix against the independent Python oracle.
    # (Full-workload Python BFS would be minutes; the prefix keeps the
    #  independent check real while staying quick.)
    log("  oracle spot-check on large workload prefix (4000 ops via python BFS)...")
    expected_prefix = python_oracle(n_big, ops_big[:4000])
    check("large prefix oracle agreement",
          connected_flags(seg_big)[:len(expected_prefix)], expected_prefix)


def main():
    global LOG_FH
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    LOG_FH = open(LOG_PATH, "w", encoding="utf-8")
    log(f"binary: {BIN}")
    if not os.path.exists(BIN):
        log("FATAL: binary missing; run `make` first")
        return 2
    log(f"started: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    random_differential_tests()
    fixed_cases()
    validation_cases()
    stats_cases()
    performance_cases()

    log("")
    log(f"PASSED checks: {len(PASSED)}")
    log(f"FAILED checks: {len(FAILED)}")
    if FAILED:
        log("FAILED DETAILS:")
        for name, actual, expected in FAILED:
            log(f"  - {name}: actual={actual!r} expected={expected!r}")
    LOG_FH.close()
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
