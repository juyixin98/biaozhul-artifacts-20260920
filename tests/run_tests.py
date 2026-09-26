#!/usr/bin/env python3
"""Automated tests for the small-scale SAT backend.

Covers:
  - edge cases: empty formula, empty clause, tautology, duplicate literals
  - unit propagation chains and backtracking (incl. pigeonhole 3->2)
  - randomized cross-check of DPLL against the exhaustive reference
  - independent verification of every emitted proof
  - negative tests: tampered proofs and wrong assignments must be rejected
  - limit enforcement
"""

import json
import os
import random
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "sat_backend")

failures = []
passes = 0


def run(req):
    proc = subprocess.run(
        [BIN], input=json.dumps(req), capture_output=True, text=True
    )
    try:
        return json.loads(proc.stdout), proc.returncode
    except json.JSONDecodeError:
        return {"status": "unparseable", "raw": proc.stdout,
                "stderr": proc.stderr}, proc.returncode


def check(name, cond, detail=""):
    global passes
    if cond:
        passes += 1
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name}  {detail}")


def solve(num_vars, clauses, proof=True):
    resp, _ = run({"op": "solve", "num_vars": num_vars,
                   "clauses": clauses, "proof": proof})
    return resp


def verify(num_vars, clauses, result, proof):
    resp, _ = run({"op": "verify_proof", "num_vars": num_vars,
                   "clauses": clauses, "result": result, "proof": proof})
    return resp


def check_assignment(num_vars, clauses, assignment):
    resp, _ = run({"op": "check_assignment", "num_vars": num_vars,
                   "clauses": clauses, "assignment": assignment})
    return resp


def reference(num_vars, clauses):
    resp, _ = run({"op": "reference", "num_vars": num_vars,
                   "clauses": clauses})
    return resp


def expect_sat_verified(name, num_vars, clauses):
    resp = solve(num_vars, clauses)
    check(f"{name}: status sat", resp.get("status") == "sat", str(resp))
    if resp.get("status") != "sat":
        return
    assignment = resp["assignment"]
    ca = check_assignment(num_vars, clauses, assignment)
    check(f"{name}: assignment accepted by checker",
          ca.get("valid") is True, str(ca))
    vp = verify(num_vars, clauses, "sat", resp["proof"])
    check(f"{name}: proof accepted by checker",
          vp.get("valid") is True, str(vp))


def expect_unsat_verified(name, num_vars, clauses):
    resp = solve(num_vars, clauses)
    check(f"{name}: status unsat", resp.get("status") == "unsat", str(resp))
    if resp.get("status") != "unsat":
        return
    vp = verify(num_vars, clauses, "unsat", resp["proof"])
    check(f"{name}: proof accepted by checker",
          vp.get("valid") is True, str(vp))


def main():
    build = subprocess.run(["make", "-C", ROOT], capture_output=True, text=True)
    if build.returncode != 0:
        print(build.stdout)
        print(build.stderr)
        print("BUILD FAILED")
        sys.exit(1)

    print("== edge cases ==")
    expect_sat_verified("empty formula", 0, [])
    expect_sat_verified("empty formula with spare vars", 3, [])
    expect_unsat_verified("empty clause", 2, [[]])
    expect_sat_verified("tautology only", 2, [[1, -1], [2, -2, 2]])
    expect_sat_verified("duplicate literals", 2, [[1, 1, 1], [-1, 2, 2]])
    expect_sat_verified("unit chain", 4, [[1], [-1, 2], [-2, 3], [-3, 4]])
    expect_unsat_verified("unit conflict", 1, [[1], [-1]])
    expect_unsat_verified("xor pair unsat", 2,
                          [[1, 2], [1, -2], [-1, 2], [-1, -2]])

    print("== backtracking: pigeonhole 3 pigeons / 2 holes ==")
    # var(p,h) = (p-1)*2 + h, p in 1..3, h in 1..2
    ph_clauses = [[1, 2], [3, 4], [5, 6]]  # each pigeon somewhere
    for hole in range(1, 3):               # no two pigeons share a hole
        for p1 in range(3):
            for p2 in range(p1 + 1, 3):
                ph_clauses.append([-(p1 * 2 + hole), -(p2 * 2 + hole)])
    expect_unsat_verified("pigeonhole 3->2", 6, ph_clauses)

    print("== reference oracle agreement ==")
    # (x or y) and (not x or y)  ==  y, so exactly 2 satisfying assignments.
    resp = reference(2, [[1, 2], [-1, 2]])
    check("reference counts satisfying assignments",
          resp.get("status") == "sat" and resp.get("num_satisfying") == 2,
          str(resp))

    print("== randomized fuzz: DPLL vs exhaustive reference ==")
    rng = random.Random(20260925)
    fuzz_count = 300
    for case in range(fuzz_count):
        n = rng.randint(1, 8)
        m = rng.randint(0, 12)
        clauses = []
        for _ in range(m):
            size = rng.randint(0, 4)
            clause = []
            for _ in range(size):
                var = rng.randint(1, n)
                clause.append(var if rng.random() < 0.5 else -var)
            clauses.append(clause)
        dpll = solve(n, clauses)
        ref = reference(n, clauses)
        ok = dpll.get("status") == ref.get("status")
        check(f"fuzz {case}: status match (n={n}, m={m})", ok,
              f"dpll={dpll.get('status')} ref={ref.get('status')} "
              f"clauses={clauses}")
        if not ok:
            continue
        result = dpll["status"]
        vp = verify(n, clauses, result, dpll["proof"])
        check(f"fuzz {case}: proof verifies",
              vp.get("valid") is True, str(vp))
        if result == "sat":
            ca = check_assignment(n, clauses, dpll["assignment"])
            check(f"fuzz {case}: assignment verifies",
                  ca.get("valid") is True, str(ca))
            check(f"fuzz {case}: reference agrees sat count > 0",
                  ref.get("num_satisfying", 0) > 0, str(ref))
        else:
            check(f"fuzz {case}: reference agrees count == 0",
                  ref.get("num_satisfying") == 0, str(ref))

    print("== negative: tampered proofs must be rejected ==")
    unsat_clauses = [[1, 2], [1, -2], [-1, 2], [-1, -2]]
    good = solve(2, unsat_clauses)
    proof = good["proof"]

    vp = verify(2, unsat_clauses, "unsat", proof[:-1])
    check("truncated unsat proof rejected", vp.get("valid") is False, str(vp))

    tampered = json.loads(json.dumps(proof))
    tampered[0]["value"] = True  # first decide must be false
    vp = verify(2, unsat_clauses, "unsat", tampered)
    check("flipped first decision rejected", vp.get("valid") is False, str(vp))

    vp = verify(2, unsat_clauses, "sat", proof)
    check("unsat proof claimed as sat rejected", vp.get("valid") is False,
          str(vp))

    sat_clauses = [[1, 2], [-1, 3]]
    sat_resp = solve(3, sat_clauses)
    vp = verify(3, sat_clauses, "unsat", sat_resp["proof"])
    check("sat proof claimed as unsat rejected", vp.get("valid") is False,
          str(vp))

    tampered = json.loads(json.dumps(sat_resp["proof"]))
    for ev in tampered:
        if ev["ev"] == "unit":
            ev["lit"] = -ev["lit"]
            break
    vp = verify(3, sat_clauses, "sat", tampered)
    check("flipped unit literal rejected", vp.get("valid") is False, str(vp))

    tampered = json.loads(json.dumps(sat_resp["proof"]))
    for ev in tampered:
        if ev["ev"] == "unit":
            ev["clause"] = 999
            break
    vp = verify(3, sat_clauses, "sat", tampered)
    check("bad unit clause index rejected", vp.get("valid") is False, str(vp))

    print("== negative: wrong assignments must be rejected ==")
    ca = check_assignment(2, [[1, 2]], [False, False])
    check("falsifying assignment rejected", ca.get("valid") is False, str(ca))
    ca = check_assignment(2, [[1, 2]], [False, None])
    check("partial assignment leaving clause open rejected",
          ca.get("valid") is False, str(ca))
    ca = check_assignment(2, [[1, 2]], [False, True])
    check("satisfying assignment accepted", ca.get("valid") is True, str(ca))

    print("== limits ==")
    resp, code = run({"op": "solve", "num_vars": 65, "clauses": []})
    check("num_vars > 64 rejected",
          resp.get("status") == "error" and code == 2, str(resp))
    resp, code = run({"op": "reference", "num_vars": 21, "clauses": []})
    check("reference with 21 vars rejected",
          resp.get("status") == "error" and code == 2, str(resp))
    resp, code = run({"op": "solve", "num_vars": 2, "clauses": [[3]]})
    check("literal out of range rejected",
          resp.get("status") == "error" and code == 2, str(resp))
    resp, code = run({"op": "solve", "num_vars": 2, "clauses": [[0]]})
    check("literal 0 rejected",
          resp.get("status") == "error" and code == 2, str(resp))
    resp, _ = run({"op": "limits"})
    check("limits op works", resp.get("max_vars") == 64, str(resp))

    print()
    total = passes + len(failures)
    print(f"RESULT: {passes}/{total} checks passed")
    if failures:
        print("FAILED:")
        for name in failures:
            print(f"  - {name}")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
