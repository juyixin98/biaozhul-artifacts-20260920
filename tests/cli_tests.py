#!/usr/bin/env python3
"""End-to-end CLI tests for domtree_backend.

Runs the compiled binary as a subprocess, feeds JSON requests via
files or stdin, and asserts on the JSON responses and exit codes.
Also feeds every tests/requests/*.json file through the binary and
checks success/naive verification.
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REQUESTS_DIR = os.path.join(HERE, "requests")

failures = []
checks = 0


def check(cond, what):
    global checks
    checks += 1
    if not cond:
        failures.append(what)
        print(f"FAIL: {what}")


def run(bin_path, payload, use_file=False):
    if use_file:
        proc = subprocess.run(
            [bin_path, "-f", payload], capture_output=True, text=True
        )
    else:
        proc = subprocess.run(
            [bin_path], input=payload, capture_output=True, text=True
        )
    return proc.returncode, proc.stdout, proc.stderr


def run_json(bin_path, obj, use_file=False, raw=None):
    text = raw if raw is not None else json.dumps(obj)
    code, out, err = run(bin_path, text, use_file=use_file)
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        parsed = None
    return code, parsed, out, err


def test_diamond_with_frontier(bin_path):
    req = {
        "entry": "A",
        "nodes": ["A", "B", "C", "D"],
        "edges": [["A", "B"], ["A", "C"], ["B", "D"], ["C", "D"]],
        "verify_naive": True,
    }
    code, resp, _, _ = run_json(bin_path, req)
    check(code == 0, "diamond: exit code 0")
    check(resp and resp.get("success") is True, "diamond: success")
    if resp:
        idoms = {
            row["node"]: row.get("idom")
            for row in resp["result"]["immediate_dominators"]
        }
        check(idoms == {"A": None, "B": "A", "C": "A", "D": "A"},
              "diamond: idom table")
        df = resp["result"]["dominance_frontiers"]
        check(df["B"] == ["D"] and df["C"] == ["D"],
              "diamond: frontiers {D}")
        check(resp["naive_verification"]["match"] is True,
              "diamond: naive verification matches")
        # Dominator sets evidence.
        check(set(resp["result"]["dominator_sets"]["D"]) == {"A", "D"},
              "diamond: Dom(D) = {A,D}")


def test_loop_unreachable_multi_exit(bin_path):
    # A->B; B->{C,D}; C->B back edge; D->E and D->F (two exits);
    # disconnected cycle X<->Y.
    req = {
        "entry": "A",
        "nodes": ["A", "B", "C", "D", "E", "F", "X", "Y"],
        "edges": [
            ["A", "B"], ["B", "C"], ["B", "D"], ["C", "B"],
            ["D", "E"], ["D", "F"], ["X", "Y"], ["Y", "X"],
        ],
        "queries": [
            {"type": "frontier", "node": "B"},
            {"type": "frontier", "node": "C"},
            {"type": "dom_chain", "node": "E"},
            {"type": "dominated_by", "node": "D"},
            {"type": "idom", "node": "X"},
        ],
        "verify_naive": True,
    }
    code, resp, _, _ = run_json(bin_path, req)
    check(code == 0 and resp and resp["success"], "loop: success")
    if resp:
        res = resp["result"]
        check(sorted(res["unreachable"]) == ["X", "Y"],
              "loop: X,Y unreachable")
        back = res["evidence"]["back_edges"]
        check(back == [["C", "B"]], f"loop: back edge C->B, got {back}")
        unr_edges = res["evidence"]["edges_touching_unreachable"]
        check(sorted(map(tuple, unr_edges)) == [("X", "Y"), ("Y", "X")],
              "loop: unreachable cycle edges reported")
        df = res["dominance_frontiers"]
        check(df["B"] == ["B"], f"loop: DF(B)={{B}}, got {df['B']}")
        check(df["C"] == ["B"], f"loop: DF(C)={{B}}, got {df['C']}")
        idoms = {r["node"]: r.get("idom")
                 for r in res["immediate_dominators"]}
        check(idoms["E"] == "D" and idoms["F"] == "D",
              "loop: both exits dominated by D")
        qs = {q["type"] + ":" + q.get("node", ""): q for q in resp["queries"]}
        check(qs["frontier:B"]["frontier"] == ["B"], "loop: query DF(B)")
        check(qs["dom_chain:E"]["chain"] == ["E", "D", "B", "A"],
              "loop: chain E")
        check(qs["dominated_by:D"]["subtree"] == ["D", "E", "F"],
              "loop: D subtree")
        check(qs["idom:X"]["ok"] is False and
              qs["idom:X"]["reachable"] is False,
              "loop: idom of unreachable X errors")
        check(resp["naive_verification"]["match"] is True,
              "loop: naive verification matches")


def test_self_loop(bin_path):
    req = {
        "entry": "A",
        "nodes": ["A", "B"],
        "edges": [["A", "A"], ["A", "B"]],
        "verify_naive": True,
    }
    code, resp, _, _ = run_json(bin_path, req)
    check(code == 0 and resp["success"], "self-loop: success")
    if resp:
        df = resp["result"]["dominance_frontiers"]
        check(df["A"] == ["A"], "self-loop: DF(A) contains A")
        check(resp["result"]["evidence"]["back_edges"] == [["A", "A"]],
              "self-loop: A->A back edge")
        check(resp["naive_verification"]["match"] is True,
              "self-loop: naive verification matches")


def test_error_cases(bin_path):
    code, resp, _, _ = run_json(bin_path, {"entry": "A"})
    check(code == 0 and resp["success"] is False,
          "errors: missing nodes rejected")

    code, resp, _, _ = run_json(bin_path, None, raw="not-json{{{")
    check(code == 0 and resp["success"] is False and "parse" in resp["error"],
          "errors: malformed JSON rejected")

    code, resp, _, _ = run_json(
        bin_path, {"entry": "Z", "nodes": ["A"], "edges": []}
    )
    check(resp["success"] is False and "entry" in resp["error"],
          "errors: unknown entry rejected")

    code, resp, _, _ = run_json(
        bin_path,
        {"entry": "A", "nodes": ["A", "B"], "edges": [["B", "A"]]},
    )
    check(resp["success"] is True, "errors: entry with no incoming ok")
    check(resp["result"]["reachable"] == ["A"] and
          resp["result"]["unreachable"] == ["B"],
          "errors: node only reachable via a path from B is unreachable")

    # -f with a missing file -> exit code 2.
    proc = subprocess.run(
        [bin_path, "-f", "/nonexistent/request.json"],
        capture_output=True, text=True,
    )
    check(proc.returncode == 2, "errors: missing file exit code 2")


def test_file_input_and_samples(bin_path):
    samples = sorted(
        f for f in os.listdir(REQUESTS_DIR) if f.endswith(".json")
    )
    check(len(samples) >= 3, f"samples: at least 3 samples, got {len(samples)}")
    for name in samples:
        path = os.path.join(REQUESTS_DIR, name)
        code, out, err = run(bin_path, path, use_file=True)
        check(code == 0, f"samples: {name} exit code 0")
        try:
            resp = json.loads(out)
        except json.JSONDecodeError:
            check(False, f"samples: {name} produced invalid JSON")
            continue
        check(resp.get("success") is True,
              f"samples: {name} success (error: {resp.get('error')})")
        if "naive_verification" in resp:
            nv = resp["naive_verification"]
            check(nv.get("ran") and nv.get("match") is True,
                  f"samples: {name} naive verification matches")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True, help="path to domtree_backend")
    args = ap.parse_args()

    if not os.path.exists(args.bin):
        print(f"binary not found: {args.bin}", file=sys.stderr)
        return 2

    test_diamond_with_frontier(args.bin)
    test_loop_unreachable_multi_exit(args.bin)
    test_self_loop(args.bin)
    test_error_cases(args.bin)
    test_file_input_and_samples(args.bin)

    print(("ALL CLI TESTS PASSED" if not failures else "CLI TESTS FAILED") +
          f" ({checks - len(failures)}/{checks} checks)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
