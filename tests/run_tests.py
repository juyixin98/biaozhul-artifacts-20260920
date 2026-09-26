#!/usr/bin/env python3
"""Automated test harness for the mincut-backend.

Layers of evidence:
  1. Runs the C++ unit-test binary (algorithms + verifier negative tests).
  2. Fixed CLI cases with hand-computed answers (parallel edges, zero
     capacity, original reverse edges, self loops, disconnected sink).
  3. Randomized differential testing on hundreds of small graphs. For each
     one the value is checked FOUR ways:
       - the C++ Dinic max-flow value,
       - the C++ independent verifier (capacity/conservation/cut),
       - the C++ brute-force partition enumeration,
       - an INDEPENDENT brute-force enumeration written here in Python
         (plus independent Python capacity/conservation checks).
  4. Error/validation cases (bad JSON, negative capacity, scale limits...).
  5. One large-scale performance case exercising int64 capacities.

Uses only the Python standard library. Exit status is non-zero if any test
fails.
"""

import itertools
import json
import os
import random
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "mincut-backend")
UNIT_BIN = os.path.join(ROOT, "build", "unit_tests")

failures = []
passed = 0


def check(cond, name, detail=""):
    global passed
    if cond:
        passed += 1
    else:
        failures.append((name, detail))
        print("  FAIL: " + name + ((" :: " + detail) if detail else ""))


def run_cli(payload):
    """Feeds a Python object (serialized to JSON) to the backend on stdin.
    Returns (returncode, parsed_json_or_None)."""
    proc = subprocess.run(
        [BIN], input=json.dumps(payload), capture_output=True, text=True
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = None
    return proc.returncode, out, proc.stderr


def run_raw(text):
    proc = subprocess.run(
        [BIN], input=text, capture_output=True, text=True
    )
    return proc.returncode, proc.stdout


# ---------------------------------------------------------------------------
# Independent Python reference: enumerate every s-t partition.
# ---------------------------------------------------------------------------

def py_brute_force(n, s, t, edges):
    """Min cut value by exhaustive enumeration of 2^(n-2) partitions."""
    internal = [v for v in range(n) if v not in (s, t)]
    best = None
    for mask in range(1 << len(internal)):
        side = [False] * n
        side[s] = True
        for i, v in enumerate(internal):
            side[v] = bool((mask >> i) & 1)
        value = 0
        for e in edges:
            if side[e["from"]] and not side[e["to"]]:
                value += e["capacity"]
        if best is None or value < best:
            best = value
    return best


def py_independent_verification(n, s, t, edges, flow_by_id, source_side):
    """Returns (ok, flow_value, cut_value). Recomputes everything itself."""
    # Capacity constraints.
    for e in edges:
        f = flow_by_id[e["id"]]
        if not (0 <= f <= e["capacity"]):
            return False, None, None
    # Conservation.
    balance = [0] * n
    for e in edges:
        f = flow_by_id[e["id"]]
        balance[e["from"]] += f
        balance[e["to"]] -= f
    for v in range(n):
        if v not in (s, t) and balance[v] != 0:
            return False, None, None
    flow_value = balance[s]
    if flow_value != -balance[t]:
        return False, None, None
    if not source_side[s] or source_side[t]:
        return False, None, None
    # Cut value from the reported partition.
    cut_value = 0
    for e in edges:
        if source_side[e["from"]] and not source_side[e["to"]]:
            cut_value += e["capacity"]
    return True, flow_value, cut_value


# ---------------------------------------------------------------------------
# Fixed CLI cases
# ---------------------------------------------------------------------------

def fixed_case(name, payload, expected_value):
    payload = dict(payload, bruteforce=True)
    rc, out, err = run_cli(payload)
    ok = (
        rc == 0
        and out is not None
        and out.get("status") == "ok"
        and out.get("max_flow_value") == expected_value
        and out["min_cut"]["cut_value"] == expected_value
        and out["verification"]["ok"] is True
        and out["bruteforce"]["matches_max_flow"] is True
        and out["bruteforce"]["min_cut_value"] == expected_value
    )
    check(ok, "fixed: " + name,
          "" if ok else json.dumps(out)[:400] if out else err)


def test_fixed_cases():
    fixed_case(
        "diamond",
        {"num_vertices": 4, "source": 0, "sink": 3,
         "edges": [
             {"id": "a", "from": 0, "to": 1, "capacity": 3},
             {"id": "b", "from": 0, "to": 2, "capacity": 2},
             {"id": "d", "from": 1, "to": 3, "capacity": 2},
             {"id": "e", "from": 2, "to": 3, "capacity": 3}]},
        4,
    )
    fixed_case(
        "parallel edges",
        {"num_vertices": 3, "source": 0, "sink": 2,
         "edges": [
             {"id": "p1", "from": 0, "to": 1, "capacity": 4},
             {"id": "p2", "from": 0, "to": 1, "capacity": 3},
             {"id": "e", "from": 1, "to": 2, "capacity": 5}]},
        5,
    )
    fixed_case(
        "zero capacity edges",
        {"num_vertices": 3, "source": 0, "sink": 2,
         "edges": [
             {"id": "z", "from": 0, "to": 1, "capacity": 0},
             {"id": "e", "from": 1, "to": 2, "capacity": 5},
             {"id": "d", "from": 0, "to": 2, "capacity": 2}]},
        2,
    )
    fixed_case(
        "original reverse edge present",
        {"num_vertices": 3, "source": 0, "sink": 2,
         "edges": [
             {"id": "f", "from": 0, "to": 1, "capacity": 5},
             {"id": "r", "from": 1, "to": 0, "capacity": 3},
             {"id": "o", "from": 1, "to": 2, "capacity": 5}]},
        5,
    )
    fixed_case(
        "self loops harmless",
        {"num_vertices": 3, "source": 0, "sink": 2,
         "edges": [
             {"id": "loop", "from": 1, "to": 1, "capacity": 9},
             {"id": "a", "from": 0, "to": 1, "capacity": 4},
             {"id": "b", "from": 1, "to": 2, "capacity": 4}]},
        4,
    )
    fixed_case(
        "disconnected sink",
        {"num_vertices": 3, "source": 0, "sink": 2,
         "edges": [{"id": "a", "from": 0, "to": 1, "capacity": 4}]},
        0,
    )
    fixed_case(
        "single saturated edge",
        {"num_vertices": 2, "source": 0, "sink": 1,
         "edges": [{"id": "a", "from": 0, "to": 1, "capacity": 7}]},
        7,
    )
    fixed_case(
        "bidirectional s-t pair",
        {"num_vertices": 2, "source": 0, "sink": 1,
         "edges": [
             {"id": "f", "from": 0, "to": 1, "capacity": 6},
             {"id": "b", "from": 1, "to": 0, "capacity": 2}]},
        6,
    )


# ---------------------------------------------------------------------------
# Randomized differential tests
# ---------------------------------------------------------------------------

def random_graph(rng, n):
    s, t = 0, n - 1
    edge_count = rng.randint(0, max(1, n * 3))
    edges = []
    for i in range(edge_count):
        u = rng.randrange(n)
        v = rng.randrange(n)
        # Bias capacities: frequently zero, bounded small integers.
        cap = rng.choice([0, 0, rng.randrange(0, 20)])
        edges.append({"id": f"e{i}", "from": u, "to": v, "capacity": cap})
    return {"num_vertices": n, "source": s, "sink": t, "edges": edges}


def test_random_small(iterations=400, seed=20260925):
    rng = random.Random(seed)
    for it in range(iterations):
        n = rng.randint(2, 12)
        payload = random_graph(rng, n)
        payload["bruteforce"] = True
        rc, out, err = run_cli(payload)
        tag = f"random[{it}] n={n} m={len(payload['edges'])}"
        if rc != 0 or out is None or out.get("status") != "ok":
            check(False, tag, json.dumps(out)[:300] if out else err)
            continue

        flow_value = out["max_flow_value"]
        cut_value = out["min_cut"]["cut_value"]
        ver_ok = out["verification"]["ok"]
        brute_value = out["bruteforce"]["min_cut_value"]
        py_brute = py_brute_force(
            n, payload["source"], payload["sink"], payload["edges"]
        )

        flow_by_id = {f["id"]: f["flow"] for f in out["flow"]}
        source_side = [False] * n
        for v in out["min_cut"]["source_side"]:
            source_side[v] = True
        py_ok, py_flow, py_cut = py_independent_verification(
            n, payload["source"], payload["sink"], payload["edges"],
            flow_by_id, source_side,
        )

        all_equal = (
            flow_value == cut_value == brute_value == py_brute == py_flow
            == py_cut
        )
        check(ver_ok, tag + " :: C++ verifier",
              json.dumps(out["verification"].get("failures", []))[:300])
        check(all_equal,
              tag + " :: all four value references agree",
              f"flow={flow_value} cut={cut_value} "
              f"cpp_brute={brute_value} py_brute={py_brute} "
              f"py_flow={py_flow} py_cut={py_cut}")
        check(py_ok, tag + " :: independent Python verification")
        check(out["bruteforce"]["partitions_checked"] == 1 << (n - 2),
              tag + " :: partition count")

        # A self loop can never carry flow in a feasible solution produced by
        # Dinic: the level-graph step cannot use an edge u->u.
        self_loop_bad = [f["id"] for f in out["flow"]
                         if f["from"] == f["to"] and f["flow"] != 0]
        check(not self_loop_bad, tag + " :: self loops carry zero flow",
              ",".join(self_loop_bad))


# ---------------------------------------------------------------------------
# Error / validation tests
# ---------------------------------------------------------------------------

def expect_error(name, payload, expected_code=None, rc=0):
    got_rc, out, err = run_cli(payload)
    ok = got_rc == rc and out is not None and out.get("status") == "error"
    if expected_code is not None:
        ok = ok and out.get("error_code") == expected_code
    check(ok, "error: " + name, json.dumps(out)[:300] if out else err)


def test_errors():
    expect_error(
        "negative capacity",
        {"num_vertices": 2, "source": 0, "sink": 1,
         "edges": [{"id": "a", "from": 0, "to": 1, "capacity": -1}]},
        "INVALID_REQUEST")
    expect_error(
        "source equals sink",
        {"num_vertices": 2, "source": 0, "sink": 0,
         "edges": []}, "INVALID_REQUEST")
    expect_error(
        "vertex out of range",
        {"num_vertices": 2, "source": 0, "sink": 1,
         "edges": [{"id": "a", "from": 0, "to": 5, "capacity": 1}]},
        "INVALID_REQUEST")
    expect_error(
        "duplicate edge id",
        {"num_vertices": 2, "source": 0, "sink": 1,
         "edges": [
             {"id": "a", "from": 0, "to": 1, "capacity": 1},
             {"id": "a", "from": 0, "to": 1, "capacity": 2}]},
        "INVALID_REQUEST")
    expect_error(
        "missing edges field",
        {"num_vertices": 2, "source": 0, "sink": 1}, "INVALID_REQUEST")
    expect_error(
        "non-integer capacity",
        {"num_vertices": 2, "source": 0, "sink": 1,
         "edges": [{"id": "a", "from": 0, "to": 1, "capacity": 1.5}]},
        "INVALID_REQUEST")
    expect_error(
        "string capacity",
        {"num_vertices": 2, "source": 0, "sink": 1,
         "edges": [{"id": "a", "from": 0, "to": 1, "capacity": "3"}]},
        "INVALID_REQUEST")
    expect_error(
        "num_vertices too large",
        {"num_vertices": 10001, "source": 0, "sink": 1, "edges": []},
        "SCALE_LIMIT")
    expect_error(
        "capacity too large",
        {"num_vertices": 2, "source": 0, "sink": 1,
         "edges": [{"id": "a", "from": 0, "to": 1,
                    "capacity": 1_000_000_001}]},
        "SCALE_LIMIT")
    expect_error(
        "bruteforce too large",
        {"num_vertices": 19, "source": 0, "sink": 18, "bruteforce": True,
         "edges": []},
        "BRUTE_TOO_LARGE")
    expect_error(
        "request not an object",
        [1, 2, 3], "INVALID_REQUEST")

    # Malformed JSON -> exit code 2.
    rc, raw = run_raw("{ not json")
    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        pass
    check(rc == 2 and parsed is not None
          and parsed.get("error_code") == "MALFORMED_JSON",
          "error: malformed JSON exits 2", raw[:200])


# ---------------------------------------------------------------------------
# Large-scale / int64 performance test
# ---------------------------------------------------------------------------

def test_large_scale():
    rng = random.Random(424242)
    layers, width = 60, 60
    n = layers * width
    s = 0
    t = n - 1
    edges = []
    idx = 0
    # Dense forward edges between consecutive layers.
    for layer in range(layers - 1):
        for u_base in range(width):
            u = layer * width + u_base
            targets = rng.sample(range(width), min(12, width))
            for wb in targets:
                v = (layer + 1) * width + wb
                # Large capacities so totals exceed 32-bit range.
                cap = rng.randrange(0, 1_000_000_000)
                edges.append({"id": f"e{idx}", "from": u, "to": v,
                              "capacity": cap})
                idx += 1
    payload = {"num_vertices": n, "source": s, "sink": t, "edges": edges}
    start = time.time()
    rc, out, err = run_cli(payload)
    elapsed = time.time() - start
    ok = (rc == 0 and out is not None and out.get("status") == "ok"
          and out["verification"]["ok"] is True
          and out["max_flow_value"] == out["min_cut"]["cut_value"]
          and out["max_flow_value"] > 2**31)
    check(ok,
          f"large scale n={n} m={len(edges)} in {elapsed:.2f}s, "
          "value exceeds int32",
          json.dumps(out)[:300] if not ok and out else err)
    print(f"    (large instance: {n} vertices, {len(edges)} edges, "
          f"{elapsed:.2f}s, value={out['max_flow_value'] if out else '?'})")


def main():
    print("== C++ unit tests ==")
    if not os.path.exists(UNIT_BIN):
        print("unit test binary missing; build it first")
        return 2
    proc = subprocess.run([UNIT_BIN])
    check(proc.returncode == 0, "C++ unit test binary exit status",
          f"exit={proc.returncode}")

    print("== fixed CLI cases ==")
    test_fixed_cases()

    print("== randomized differential tests ==")
    test_random_small()

    print("== error/validation tests ==")
    test_errors()

    print("== large scale test ==")
    test_large_scale()

    print()
    print(f"RESULT: {passed} passed, {len(failures)} failed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
