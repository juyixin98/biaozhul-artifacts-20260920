#!/usr/bin/env python3
"""Automated integration tests for the mincut backend.

Strategy:
  * `solve` output is checked by the independent C++ `verify` subcommand
    (conservation, capacity bounds, exact cut set, cut==flow, residual
    reachability, per-edge residual arc bookkeeping).
  * For every small graph we also compare against the exhaustive `brute`
    oracle (complete enumeration of all s-t partitions).
  * Tampered responses must be REJECTED by the verifier.
  * Malformed requests must produce clean error responses.

No third-party Python packages are required (stdlib only).
"""

import json
import os
import random
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "build", "mincut")
EXAMPLES = os.path.join(ROOT, "examples")

PASSED = 0
FAILED = 0
FAILURES = []


def run(mode, payload):
    """Runs `mincut <mode>` with payload (dict -> JSON) on stdin."""
    proc = subprocess.run(
        [BIN, mode, "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = None
    return proc.returncode, out, proc.stderr


def run_raw(mode, text):
    proc = subprocess.run(
        [BIN, mode, "-"], input=text, capture_output=True, text=True, timeout=30
    )
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode, None


def expect(cond, name, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"FAIL: {name} {detail}")


def verify_response(response):
    proc = subprocess.run(
        [BIN, "verify", "-"],
        input=json.dumps(response),
        capture_output=True,
        text=True,
        timeout=30,
    )
    report = json.loads(proc.stdout)
    return proc.returncode == 0, report


def solve_and_verify(req, name):
    rc, resp, err = run("solve", req)
    expect(rc == 0 and resp is not None and resp.get("ok") is True,
           f"{name}: solve succeeds", err)
    if resp is None:
        return None
    valid, report = verify_response(resp)
    expect(valid, f"{name}: independent verification passes",
           json.dumps(report.get("errors", [])))
    return resp


def random_graph(rng, n, allow_parallel=True, allow_self_loop=True):
    nodes = [f"n{i}" for i in range(n)]
    s, t = nodes[0], nodes[-1]
    edges = []
    used = set()
    # Guarantee some chance of connectivity.
    for i in range(n - 1):
        cap = rng.randint(0, 8)
        edges.append({"from": nodes[i], "to": nodes[i + 1], "capacity": cap})
        used.add((nodes[i], nodes[i + 1]))
    for _ in range(rng.randint(0, 2 * n)):
        u, v = rng.randrange(n), rng.randrange(n)
        if u == v and not allow_self_loop:
            continue
        key = (nodes[u], nodes[v])
        if key in used and not allow_parallel:
            continue
        edges.append({"from": nodes[u], "to": nodes[v],
                      "capacity": rng.randint(0, 8)})
        used.add(key)
    return {"nodes": nodes, "edges": edges, "source": s, "sink": t}


def test_examples():
    for fname in sorted(os.listdir(EXAMPLES)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(EXAMPLES, fname), encoding="utf-8") as f:
            req = json.load(f)
        resp = solve_and_verify(req, f"example/{fname}")
        if resp is None:
            continue
        if len(req["nodes"]) <= 20:
            rc, brute, _ = run("brute", req)
            expect(rc == 0, f"example/{fname}: brute runs")
            expect(
                brute["min_cut_value"] == resp["max_flow"],
                f"example/{fname}: flow == exhaustive cut",
                f"{resp['max_flow']} vs {brute['min_cut_value']}",
            )
            expect(
                brute["partitions_checked"] == 2 ** (len(req["nodes"]) - 2),
                f"example/{fname}: brute enumerates 2^(n-2) partitions",
            )


def test_random_exhaustive():
    rng = random.Random(20260925)
    for trial in range(250):
        n = rng.randint(2, 10)
        req = random_graph(rng, n)
        name = f"random#{trial}(n={n})"
        rc, resp, err = run("solve", req)
        if rc != 0:
            expect(False, f"{name}: solve", err)
            continue
        valid, report = verify_response(resp)
        expect(valid, f"{name}: verify", json.dumps(report.get("errors", [])))
        rc2, brute, _ = run("brute", req)
        expect(rc2 == 0 and brute is not None, f"{name}: brute")
        if brute:
            expect(brute["min_cut_value"] == resp["max_flow"],
                   f"{name}: flow equals exhaustive min cut",
                   f"flow={resp['max_flow']} cut={brute['min_cut_value']}")
            # The brute witness partition itself must have that capacity;
            # recompute independently here in Python.
            cap = sum(e["capacity"] for e in req["edges"]
                      if e["from"] in brute["source_side"]
                      and e["to"] in brute["sink_side"])
            expect(cap == brute["min_cut_value"],
                   f"{name}: brute witness cut capacity consistent")


def test_known_values():
    # Classic CLRS network, max flow = 23.
    clrs = {
        "nodes": ["s", "v1", "v2", "v3", "v4", "t"],
        "edges": [
            {"id": "a", "from": "s", "to": "v1", "capacity": 16},
            {"id": "b", "from": "s", "to": "v2", "capacity": 13},
            {"id": "c", "from": "v1", "to": "v2", "capacity": 10},
            {"id": "d", "from": "v2", "to": "v1", "capacity": 4},
            {"id": "e", "from": "v1", "to": "v3", "capacity": 12},
            {"id": "f", "from": "v3", "to": "v2", "capacity": 9},
            {"id": "g", "from": "v2", "to": "v4", "capacity": 14},
            {"id": "h", "from": "v4", "to": "v3", "capacity": 7},
            {"id": "i", "from": "v3", "to": "t", "capacity": 20},
            {"id": "j", "from": "v4", "to": "t", "capacity": 4},
        ],
        "source": "s", "sink": "t",
    }
    resp = solve_and_verify(clrs, "clrs")
    expect(resp and resp["max_flow"] == 23, "clrs max flow = 23",
           str(resp and resp["max_flow"]))
    cut_ids = set(resp["cut"]["cut_edges"])
    # Textbook cut S = {s, v1, v2, v4}: edges e(v1->v3)=12, h(v4->v3)=7,
    # j(v4->t)=4, total 23.
    expect(cut_ids == {"e", "h", "j"}, "clrs cut edges are e,h,j (23)", str(cut_ids))

    # Parallel edges + zero capacity + a reverse-direction edge.
    mix = {
        "nodes": ["s", "a", "t"],
        "edges": [
            {"id": "p1", "from": "s", "to": "a", "capacity": 5},
            {"id": "p2", "from": "s", "to": "a", "capacity": 5},
            {"id": "z", "from": "a", "to": "t", "capacity": 0},
            {"id": "back", "from": "t", "to": "a", "capacity": 9},
            {"id": "d", "from": "s", "to": "t", "capacity": 4},
        ],
        "source": "s", "sink": "t",
    }
    resp = solve_and_verify(mix, "mix")
    expect(resp and resp["max_flow"] == 4, "mix: zero edge blocks path, direct=4",
           str(resp and resp["max_flow"]))

    # Residual evidence: p1 and p2 each have their own forward + reverse arcs.
    if resp:
        arcs = [(arc["edge_id"], arc["kind"])
                for node in resp["residual"]["nodes"] for arc in node["arcs"]]
        for eid in ["p1", "p2", "z", "back", "d"]:
            expect(arcs.count((eid, "forward")) == 1,
                   f"residual: exactly one forward arc for {eid}")
            expect(arcs.count((eid, "artificial_reverse")) == 1,
                   f"residual: exactly one artificial reverse arc for {eid}")


def test_tamper_detection():
    req = random_graph(random.Random(7), 5)
    rc, resp, _ = run("solve", req)
    assert rc == 0

    def tamper_and_expect_rejected(name, mutate):
        bad = json.loads(json.dumps(resp))
        mutate(bad)
        valid, report = verify_response(bad)
        expect(not valid, f"tamper rejected: {name}",
               json.dumps(report.get("errors", [])))

    tamper_and_expect_rejected(
        "inflated max_flow", lambda b: b.__setitem__("max_flow", b["max_flow"] + 1))
    tamper_and_expect_rejected(
        "inflated cut_value",
        lambda b: b["cut"].__setitem__("cut_value", b["cut"]["cut_value"] + 1))

    def over_capacity(b):
        eid = b["flows"][0]["edge_id"]
        b["flows"][0]["flow"] += 1
        # keep max_flow internally consistent-ish to isolate the bound check
        b["max_flow"] = b["flows"][0]["flow"]
        return None
    tamper_and_expect_rejected("flow over capacity", over_capacity)

    def fake_cut_set(b):
        b["cut"]["cut_edges"] = []  # omit real crossing edges
    tamper_and_expect_rejected("cut set omissions", fake_cut_set)

    def sink_in_source(b):
        b["cut"]["source_side"].append(b["request"]["sink"])
    tamper_and_expect_rejected("sink placed on both sides", sink_in_source)

    def duplicate_reverse_kind(b):
        # Relabel a forward arc as reverse: counts of own arcs break.
        for node in b["residual"]["nodes"]:
            for arc in node["arcs"]:
                if arc["kind"] == "forward":
                    arc["kind"] = "artificial_reverse"
                    break
            else:
                continue
            break
    tamper_and_expect_rejected("residual arc kind relabeled", duplicate_reverse_kind)


def test_error_handling():
    def expect_error(name, payload_or_text, raw=False):
        if raw:
            rc, out = run_raw("solve", payload_or_text)
        else:
            rc, out, _ = run("solve", payload_or_text)
        expect(rc != 0 and out is not None and out.get("ok") is False,
               f"error handled: {name}", str(out))

    expect_error("invalid json", "{not json", raw=True)
    expect_error("negative capacity",
                 {"nodes": ["s", "t"], "edges": [{"from": "s", "to": "t", "capacity": -1}],
                  "source": "s", "sink": "t"})
    expect_error("unknown endpoint",
                 {"nodes": ["s", "t"], "edges": [{"from": "s", "to": "x", "capacity": 1}],
                  "source": "s", "sink": "t"})
    expect_error("source equals sink",
                 {"nodes": ["s", "t"], "edges": [], "source": "s", "sink": "s"})
    expect_error("duplicate node",
                 {"nodes": ["s", "s", "t"], "edges": [], "source": "s", "sink": "t"})
    expect_error("duplicate edge id",
                 {"nodes": ["s", "a", "t"],
                  "edges": [{"id": "x", "from": "s", "to": "a", "capacity": 1},
                            {"id": "x", "from": "a", "to": "t", "capacity": 1}],
                  "source": "s", "sink": "t"})
    expect_error("too many nodes",
                 {"nodes": [f"n{i}" for i in range(201)],
                  "edges": [], "source": "n0", "sink": "n1"})

    # Brute refuses oversized inputs rather than hanging on 2^(n-2).
    big = {"nodes": [f"n{i}" for i in range(21)], "edges": [],
           "source": "n0", "sink": "n1"}
    rc, out, _ = run("brute", big)
    expect(rc != 0 and out.get("ok") is False, "brute size limit enforced", str(out))


def test_large_bounded():
    # Near the solver limit: 200 nodes, 1000 edges; must finish quickly and
    # still verify. Not sent to brute (exponential).
    rng = random.Random(99)
    n, m = 200, 1000
    nodes = [f"n{i}" for i in range(n)]
    edges = []
    for k in range(m):
        u = rng.randrange(n - 1)
        v = rng.randrange(u + 1, n)
        edges.append({"from": nodes[u], "to": nodes[v],
                      "capacity": rng.randint(0, 10**9)})
    req = {"nodes": nodes, "edges": edges, "source": nodes[0], "sink": nodes[-1]}
    resp = solve_and_verify(req, "large200")
    expect(resp is not None, "large bounded graph solved")


def main():
    if not os.path.exists(BIN):
        print(f"binary not found at {BIN}; run `make` first", file=sys.stderr)
        return 2
    test_examples()
    test_known_values()
    test_random_exhaustive()
    test_tamper_detection()
    test_error_handling()
    test_large_bounded()
    print(f"\nPython integration tests: {PASSED} passed, {FAILED} failed")
    if FAILED:
        print("Failures:")
        for f in FAILURES:
            print(" -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
