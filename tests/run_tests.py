#!/usr/bin/env python3
"""End-to-end / property tests for the difference-constraints backend.

These tests are deliberately independent of the C++ implementation:

* feasibility is recomputed in Python with a separately written Bellman-Ford
  and, for tiny systems, by exhaustive enumeration;
* every returned assignment is checked directly against every constraint;
* every negative-cycle witness is re-summed;
* every minimal candidate is re-proved infeasible and irreducible.

Only the Python standard library is used.
"""
import argparse
import itertools
import json
import os
import random
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BINARY = os.path.join(ROOT, "build", "diffcon_solver")
EXAMPLES = os.path.join(ROOT, "examples")

FAILS = []
CHECKS = 0


def check(cond, name):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"ok:   {name}")
    else:
        FAILS.append(name)
        print(f"FAIL: {name}")


def run_cli(body=None, file=None, extra_args=None):
    args = [BINARY]
    if file is not None:
        args.append(file)
    if extra_args:
        args += extra_args
    p = subprocess.run(args, input=body, capture_output=True, text=True, timeout=120)
    return p


def solve(body):
    p = run_cli(body=json.dumps(body))
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode, None


# ---------- Independent reference implementations ----------

def py_bellman_ford(n, edges):
    """edges: list of (x, y, c) meaning x - y <= c. Returns (feasible, dist)."""
    dist = [0] * n
    for _ in range(n):
        changed = False
        for x, y, c in edges:
            if dist[y] + c < dist[x]:
                dist[x] = dist[y] + c
                changed = True
        if not changed:
            return True, dist
    return False, dist


def py_brute_force(n, edges, cap):
    """Exhaustive search over [-cap, cap]^n."""
    for vals in itertools.product(range(-cap, cap + 1), repeat=n):
        if all(vals[x] - vals[y] <= c for x, y, c in edges):
            return True
    return False


# ---------- Response verification ----------

def verify_feasible_response(req, resp):
    names = resp["variables"]
    expected_names = set(req.get("variables", []))
    for con in req["constraints"]:
        expected_names.add(con["x"])
        expected_names.add(con["y"])
    check(set(names) == expected_names, "response lists exactly all variables")

    assignment = resp["assignment"]
    ok = True
    for con in req["constraints"]:
        if assignment[con["x"]] - assignment[con["y"]] > int(con["c"]):
            ok = False
    check(ok, "returned assignment satisfies every constraint x - y <= c")
    check(resp["verification"]["all_satisfied"] is True, "verification.all_satisfied is true")
    check(resp["verification"]["checked_constraints"] == len(req["constraints"]),
          "verification checked all constraints")

    if req.get("options", {}).get("normalize", True):
        comps = resp["normalization"]["components"]
        mins = [min(assignment[v] for v in comp) for comp in comps]
        check(all(m == 0 for m in mins), "every component normalized to min 0")
        all_members = [v for comp in comps for v in comp]
        check(sorted(all_members) == sorted(names), "normalization components partition variables")
        check(all(isinstance(v, int) for v in assignment.values()), "all assigned values are integers")


def verify_infeasible_response(req, resp):
    cyc = resp["negative_cycle"]
    by_id = {c.get("id", str(i)): c for i, c in enumerate(req["constraints"])}
    total = 0
    last_x = None
    first_y = None
    for cid in cyc["constraint_ids"]:
        con = by_id[cid]
        total += int(con["c"])  # sum telescopes to 0 along a closed cycle
        if last_x is not None:
            check(con["y"] == last_x, f"cycle edge {cid} continues from previous head")
        last_x = con["x"]
        if first_y is None:
            first_y = con["y"]
    check(total < 0, f"witness cycle sum {total} is negative")
    check(total == cyc["sum_bounds"], "reported sum_bounds matches independent sum")
    check(last_x == first_y and cyc["closes"], "witness is a closed variable cycle")

    if "minimal_candidate" in resp:
        mc = resp["minimal_candidate"]
        members = set(mc["constraint_ids"])
        sub = [by_id[cid] for cid in members]
        n = len(resp["variables"])
        names = resp["variables"]
        idx = {name: i for i, name in enumerate(names)}
        sub_edges = [(idx[c["x"]], idx[c["y"]], int(c["c"])) for c in sub]
        feasible, _ = py_bellman_ford(n, sub_edges)
        check(feasible is False, "minimal candidate subset is independently infeasible")
        irreducible = True
        for drop in members:
            fewer_edges = [(idx[c["x"]], idx[c["y"]], int(c["c"]))
                           for c in sub if c is not by_id[drop]]
            f, _ = py_bellman_ford(n, fewer_edges)
            if not f:
                irreducible = False
        check(irreducible, "removing any member makes the rest feasible (irreducible)")
        check(mc["verification"]["subset_infeasible"] is True, "service also reports subset infeasible")
        check(mc["verification"]["irreducible"] is True, "service also reports irreducible")


# ---------- Example-based tests ----------

def test_examples():
    with open(os.path.join(EXAMPLES, "01_feasible_disconnected.json")) as f:
        req = json.load(f)
    rc, resp = solve(req)
    check(rc == 0 and resp["feasible"] is True, "example 01 feasible, exit 0")
    verify_feasible_response(req, resp)
    check(resp["assignment"]["isolated"] == 0, "isolated variable assigned 0")
    check(resp["sizes"]["num_components"] == 2, "two components (linked vars + isolated)")
    check(resp["evidence"]["reference"]["verdict_matches_primary"] is True, "Floyd cross-check matches")

    with open(os.path.join(EXAMPLES, "02_zero_cycle.json")) as f:
        req = json.load(f)
    rc, resp = solve(req)
    check(rc == 0 and resp["feasible"] is True, "example 02 zero-weight cycle feasible")
    a = resp["assignment"]
    check(a["a"] - a["b"] == 1 and a["b"] - a["c"] == 1, "zero cycle forces the two tight equalities")
    verify_feasible_response(req, resp)

    with open(os.path.join(EXAMPLES, "03_negative_cycle.json")) as f:
        req = json.load(f)
    rc, resp = solve(req)
    check(rc == 0 and resp["feasible"] is False, "example 03 infeasible, still exit 0")
    verify_infeasible_response(req, resp)
    members = set(resp["minimal_candidate"]["constraint_ids"])
    check(members == {"r1", "r2", "r3"}, "minimal contradiction candidate is exactly {r1,r2,r3}")
    check("r4" not in members and "r5" not in members, "unrelated constraints excluded from candidate")
    check(resp["minimal_candidate"]["method"] == "deletion_filter", "deletion filter used at small scale")

    with open(os.path.join(EXAMPLES, "04_two_disjoint_cycles.json")) as f:
        req = json.load(f)
    rc, resp = solve(req)
    check(resp["feasible"] is False, "example 04 infeasible")
    members = set(resp["minimal_candidate"]["constraint_ids"])
    check(members in ({"cycA1", "cycA2"}, {"cycB1", "cycB2"}),
          "candidate isolates a single 2-edge cycle (order dependent)")
    verify_infeasible_response(req, resp)

    p = run_cli(file=os.path.join(EXAMPLES, "05_invalid_request.json"))
    rc = p.returncode
    err = json.loads(p.stdout)
    check(rc == 2 and err["status"] == "error", "invalid request exits 2 with error envelope")
    check(err["error"]["code"] in ("duplicate_variable", "invalid_constraint"), "specific error code given")


# ---------- Randomized property tests ----------

def random_system(rng):
    n = 1 + rng.randrange(10)
    m = rng.randrange(2 * n + 4)
    variables = [f"v{i}" for i in range(n)]
    cons = []
    edges = []
    for k in range(m):
        x, y = rng.randrange(n), rng.randrange(n)
        c = rng.randrange(-3, 4)
        cons.append({"id": f"e{k}", "x": variables[x], "y": variables[y], "c": c})
        edges.append((x, y, c))
    return {"variables": variables, "constraints": cons}, n, edges


def test_randomized(seed, trials):
    rng = random.Random(seed)
    n_infeasible = 0
    for t in range(trials):
        req, n, edges = random_system(rng)
        py_feasible, py_dist = py_bellman_ford(n, edges)
        if not py_feasible:
            n_infeasible += 1
        if n <= 3 and len(edges) <= 5:
            brute = py_brute_force(n, edges, cap=max(6, 3 * len(edges)))
            check(brute == py_feasible, f"trial {t}: brute force agrees with Python BF")

        rc, resp = solve(req)
        check(rc == 0 and resp is not None and resp["status"] == "ok", f"trial {t}: request accepted")
        check(resp["feasible"] == py_feasible,
              f"trial {t}: C++ feasibility {resp['feasible']} == Python BF {py_feasible}")
        check(resp["evidence"]["reference"]["verdict_matches_primary"] is True,
              f"trial {t}: embedded Floyd reference agrees")
        if py_feasible:
            verify_feasible_response(req, resp)
        else:
            verify_infeasible_response(req, resp)
    check(n_infeasible > trials // 8, f"random suite generated enough infeasible cases ({n_infeasible})")


# ---------- Validation / CLI tests ----------

def test_validation():
    rc, resp = solve({"constraints": [{"x": "a", "y": "b", "c": 1.5}]})
    check(rc == 2 and resp["status"] == "error", "float bound rejected")

    rc, resp = solve({"constraints": [{"x": "a", "y": "b", "c": 10**9 + 1}]})
    check(rc == 2 and resp["error"]["code"] == "constraint_out_of_range", "|c| limit enforced")

    p = run_cli(body="{broken")
    check(p.returncode == 2, "malformed JSON exits 2")

    p = run_cli(body="[1,2,3]")
    check(p.returncode == 2 and json.loads(p.stdout)["status"] == "error", "non-object body rejected")

    rc, resp = solve({"variables": [], "constraints": []})
    check(rc == 0 and resp["feasible"] is True, "empty system feasible")

    out1 = run_cli(body=json.dumps({"constraints": [{"x": "a", "y": "b", "c": 1}]}))
    out2 = run_cli(body=json.dumps({"constraints": [{"x": "a", "y": "b", "c": 1}]}))
    check(out1.stdout == out2.stdout, "identical requests produce byte-identical responses (deterministic)")

    p = run_cli(extra_args=["--help"])
    check(p.returncode == 0 and "Usage" in p.stdout, "--help exits 0 and prints usage")

    p = run_cli(file=os.path.join(EXAMPLES, "does_not_exist.json"))
    check(p.returncode == 1, "missing input file exits 1")


def test_options():
    req = {"variables": ["a", "b"],
           "constraints": [{"id": "k", "x": "a", "y": "b", "c": 1}]}
    _, raw = solve({**req, "options": {"normalize": False}})
    check(raw["normalization"]["mode"] == "raw_bellman_ford_labels", "normalize=false returns raw labels")
    _, off = solve({**req, "options": {"reference": "off"}})
    check(off["evidence"]["reference"]["used"] is False, "reference=off skips Floyd")

    # n > 300: auto skips Floyd; forced on reports why.
    n = 301
    big = {"variables": [f"v{i}" for i in range(n)],
           "constraints": [{"x": f"v{i+1}", "y": f"v{i}", "c": 0} for i in range(n - 1)]}
    _, resp_auto = solve(big)
    check(resp_auto["feasible"] is True and resp_auto["evidence"]["reference"]["used"] is False,
          "Floyd auto-skipped above n=300")
    _, resp_forced = solve({**big, "options": {"reference": "on"}})
    ref = resp_forced["evidence"]["reference"]
    check(ref["used"] is False and "limit" in ref["reason"], "forced reference over limit explains skip")


# ---------- Scale tests ----------

def large_feasible(n, m, seed):
    rng = random.Random(seed)
    cons = []
    # Chain v_{i+1} - v_i <= -1 forces distinct decreasing labels (feasible).
    for i in range(n - 1):
        cons.append({"id": f"chain{i}", "x": f"v{i+1}", "y": f"v{i}", "c": -1})
    while len(cons) < m:
        i, j = rng.randrange(n), rng.randrange(n)
        if i == j:
            continue
        # v_j - v_i = i - j by construction; allow generous slack.
        cons.append({"id": f"loose{len(cons)}", "x": f"v{j}", "y": f"v{i}", "c": n})
    rng.shuffle(cons)
    return {"variables": [f"v{i}" for i in range(n)], "constraints": cons}


def test_scale():
    req = large_feasible(2000, 8000, seed=7)
    t0 = time.time()
    rc, resp = solve(req)
    elapsed = time.time() - t0
    check(rc == 0 and resp["feasible"] is True, f"n=2000, m=8000 feasible solved (Python wall {elapsed:.2f}s)")
    check(elapsed < 30, "large instance finishes within 30s")
    a = resp["assignment"]
    ok = all(a[c["x"]] - a[c["y"]] <= c["c"] for c in req["constraints"])
    check(ok, "large instance assignment satisfies all 8000 constraints")
    check(resp["evidence"]["reference"]["used"] is False, "reference skipped at scale")

    too_many_vars = {"variables": [f"v{i}" for i in range(2001)], "constraints": []}
    rc, resp = solve(too_many_vars)
    check(rc == 2 and resp["error"]["code"] == "too_many_variables", "2001 variables rejected")

    req8001 = large_feasible(100, 1000, seed=1)  # filler
    cons = []
    for i in range(8001):
        cons.append({"id": f"e{i}", "x": "a", "y": "b", "c": 100})
    rc, resp = solve({"constraints": cons})
    check(rc == 2 and resp["error"]["code"] == "too_many_constraints", "8001 constraints rejected")

    # Infeasible at scale: fallback candidate is the raw cycle witness.
    n = 2000
    infeasible = {
        "variables": [f"v{i}" for i in range(n)],
        "constraints": ([{"id": "neg1", "x": "v0", "y": "v1", "c": 1},
                         {"id": "neg2", "x": "v1", "y": "v2", "c": 1},
                         {"id": "neg3", "x": "v2", "y": "v0", "c": -4}] +
                        [{"id": f"c{i}", "x": f"v{(i+3) % n}", "y": f"v{i % n}", "c": 1000}
                         for i in range(600)]),
    }
    t0 = time.time()
    rc, resp = solve(infeasible)
    elapsed = time.time() - t0
    check(resp["feasible"] is False, f"large infeasible instance detected ({elapsed:.2f}s)")
    check(resp["negative_cycle"]["sum_bounds"] < 0, "large case negative cycle witness valid")
    check(resp["minimal_candidate"]["method"] == "cycle_witness_fallback",
          "above MUS size limits, candidate falls back to the cycle witness")


def main():
    if not os.path.exists(BINARY):
        print(f"binary not found at {BINARY}; run 'make' first", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20260925)
    parser.add_argument("--trials", type=int, default=200)
    args = parser.parse_args()

    test_examples()
    test_randomized(args.seed, args.trials)
    test_validation()
    test_options()
    test_scale()

    print(f"\n{CHECKS} checks, {len(FAILS)} failures")
    if FAILS:
        print("FAILED:")
        for name in FAILS:
            print(f"  - {name}")
        return 1
    print("ALL PYTHON E2E TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
