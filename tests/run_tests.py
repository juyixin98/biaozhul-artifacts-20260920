#!/usr/bin/env python3
"""Automated tests for the SCC condensation backend.

Runs fixed cases (self-loops, multi-edges, isolated vertices, DAGs, empty
graph), error cases, randomized cross-validation of the Tarjan solver against
the naive reachability-matrix reference, the independent C++ verifier on every
result, and a large-graph smoke test at the configured scale limit.

Usage: python3 tests/run_tests.py   (build first with `make`)
"""

import json
import os
import random
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "build", "scc_backend")
VERIFY = os.path.join(ROOT, "build", "scc_verify")

failures = []
passes = 0


def report(ok, name, detail=""):
    global passes
    if ok:
        passes += 1
        print(f"  PASS {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name}  {detail}")


def run_backend(request, extra_args=()):
    with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False) as f:
        json.dump(request, f)
        path = f.name
    try:
        proc = subprocess.run(
            [BACKEND, *extra_args, path],
            capture_output=True, text=True, timeout=120)
    finally:
        os.unlink(path)
    return proc


def run_verify(request, result):
    with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False) as f:
        json.dump(request, f)
        req_path = f.name
    with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False) as f:
        f.write(result if isinstance(result, str) else json.dumps(result))
        res_path = f.name
    try:
        proc = subprocess.run(
            [VERIFY, req_path, res_path],
            capture_output=True, text=True, timeout=120)
    finally:
        os.unlink(req_path)
        os.unlink(res_path)
    return proc


def comp_sets(result):
    return sorted(tuple(c["vertices"]) for c in result["components"])


def cond_edges(result):
    return sorted(tuple(e) for e in result["condensation_edges"])


def check_case(name, request, expect_comps=None, expect_cond=None):
    proc = run_backend(request)
    if proc.returncode != 0:
        report(False, name, f"backend exit={proc.returncode} {proc.stdout}")
        return
    result = json.loads(proc.stdout)
    ok = True
    detail = ""
    if expect_comps is not None and comp_sets(result) != expect_comps:
        ok = False
        detail = f"components {comp_sets(result)} != {expect_comps}"
    if expect_cond is not None and cond_edges(result) != expect_cond:
        ok = False
        detail = f"condensation {cond_edges(result)} != {expect_cond}"
    vproc = run_verify(request, proc.stdout)
    if vproc.returncode != 0:
        ok = False
        detail += f" | verifier: {vproc.stdout.strip()}"
    report(ok, name, detail)


def check_error(name, request_or_text):
    if isinstance(request_or_text, str):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as f:
            f.write(request_or_text)
            path = f.name
        proc = subprocess.run([BACKEND, path],
                              capture_output=True, text=True, timeout=30)
        os.unlink(path)
    else:
        proc = run_backend(request_or_text)
    ok = proc.returncode == 2
    detail = ""
    if ok:
        try:
            out = json.loads(proc.stdout)
            ok = out.get("ok") is False and "error" in out
        except json.JSONDecodeError:
            ok = False
            detail = "error output is not JSON"
    else:
        detail = f"exit={proc.returncode} out={proc.stdout[:120]}"
    report(ok, name, detail)


def main():
    for binary in (BACKEND, VERIFY):
        if not os.path.exists(binary):
            print(f"missing binary {binary}; run `make` first")
            return 1

    print("== fixed cases ==")
    check_case("empty graph", {"vertices": 0, "edges": []},
               expect_comps=[], expect_cond=[])
    check_case("isolated vertices only", {"vertices": 4, "edges": []},
               expect_comps=[(0,), (1,), (2,), (3,)], expect_cond=[])
    check_case("self loop singleton", {"vertices": 2, "edges": [[0, 0]]},
               expect_comps=[(0,), (1,)], expect_cond=[])
    check_case("multi edges dedup",
               {"vertices": 3,
                "edges": [[0, 1], [0, 1], [0, 1], [1, 2], [1, 2]]},
               expect_comps=[(0,), (1,), (2,)],
               expect_cond=[(0, 1), (1, 2)])
    check_case("single cycle", {"vertices": 3,
                                "edges": [[0, 1], [1, 2], [2, 0]]},
               expect_comps=[(0, 1, 2)], expect_cond=[])
    check_case("two scc with parallel cross edges",
               {"vertices": 5,
                "edges": [[0, 1], [1, 2], [2, 0], [2, 3], [1, 3], [3, 4],
                          [4, 3]]},
               expect_comps=[(0, 1, 2), (3, 4)], expect_cond=[(0, 1)])
    check_case("pure dag", {"vertices": 4,
                            "edges": [[0, 1], [0, 2], [1, 3], [2, 3]]},
               expect_comps=[(0,), (1,), (2,), (3,)],
               expect_cond=[(0, 1), (0, 2), (1, 3), (2, 3)])
    check_case("complete digraph", {"vertices": 4, "edges": [
        [i, j] for i in range(4) for j in range(4) if i != j]},
               expect_comps=[(0, 1, 2, 3)], expect_cond=[])
    check_case("self loop inside larger scc",
               {"vertices": 3, "edges": [[0, 1], [1, 1], [1, 2], [2, 0]]},
               expect_comps=[(0, 1, 2)], expect_cond=[])
    check_case("mixed isolated selfloop cycle",
               {"vertices": 6,
                "edges": [[0, 0], [1, 2], [2, 1], [3, 4]]},
               expect_comps=[(0,), (1, 2), (3,), (4,), (5,)],
               expect_cond=[(2, 3)])

    print("== error cases ==")
    check_error("malformed json", "{not json")
    check_error("missing edges", {"vertices": 3})
    check_error("missing vertices", {"edges": []})
    check_error("negative vertices", {"vertices": -1, "edges": []})
    check_error("vertices over limit",
                {"vertices": 100001, "edges": []})
    check_error("edge endpoint out of range",
                {"vertices": 2, "edges": [[0, 2]]})
    check_error("negative endpoint", {"vertices": 2, "edges": [[-1, 0]]})
    check_error("edge not a pair", {"vertices": 2, "edges": [[0, 1, 2]]})
    check_error("non-integer endpoint",
                {"vertices": 2, "edges": [[0.5, 1]]})

    print("== randomized cross-validation (tarjan vs reference) ==")
    rng = random.Random(20260925)
    fuzz_ok = True
    for trial in range(300):
        n = rng.randint(0, 12)
        m = rng.randint(0, 30)
        edges = [[rng.randrange(n), rng.randrange(n)]
                 for _ in range(m)] if n else []
        request = {"vertices": n, "edges": edges}
        fast = run_backend(request)
        ref = run_backend(request, extra_args=("--reference",))
        if fast.returncode != 0 or ref.returncode != 0:
            report(False, f"fuzz#{trial}",
                   f"exit fast={fast.returncode} ref={ref.returncode}")
            fuzz_ok = False
            break
        fr, rr = json.loads(fast.stdout), json.loads(ref.stdout)
        if comp_sets(fr) != comp_sets(rr) or cond_edges(fr) != cond_edges(rr):
            report(False, f"fuzz#{trial}",
                   f"mismatch on {request}: {fast.stdout} vs {ref.stdout}")
            fuzz_ok = False
            break
        vproc = run_verify(request, fast.stdout)
        if vproc.returncode != 0:
            report(False, f"fuzz#{trial}",
                   f"verifier: {vproc.stdout.strip()} req={request}")
            fuzz_ok = False
            break
    if fuzz_ok:
        report(True, "fuzz 300 random graphs (n<=12, self-loops+multiedges)")

    print("== determinism ==")
    req = {"vertices": 8,
           "edges": [[0, 1], [1, 2], [2, 0], [3, 4], [4, 5], [5, 3],
                     [2, 3], [6, 6], [7, 0]]}
    outs = {run_backend(req).stdout for _ in range(5)}
    report(len(outs) == 1, "identical output across 5 runs")

    print("== large graph smoke test (limit size) ==")
    n = 100000
    edges = [[i, i + 1] for i in range(n - 1)]  # long chain
    edges.append([n - 1, 0])                    # one giant SCC
    edges += [[i, i] for i in range(0, n, 9973)]  # scattered self-loops
    request = {"vertices": n, "edges": edges}
    start = time.time()
    proc = run_backend(request)
    elapsed = time.time() - start
    ok = proc.returncode == 0
    detail = ""
    if ok:
        result = json.loads(proc.stdout)
        ok = (result["component_count"] == 1
              and len(result["components"][0]["vertices"]) == n)
        detail = f"components={result['component_count']}"
        vproc = run_verify(request, proc.stdout)
        if vproc.returncode != 0:
            ok = False
            detail += f" | verifier: {vproc.stdout.strip()}"
        elif "maximality=skipped" not in vproc.stdout:
            ok = False
            detail += " | expected maximality skip at n=100000"
    report(ok, f"chain+loop n=100000 in {elapsed:.2f}s", detail)

    print(f"\n{passes} passed, {len(failures)} failed")
    if failures:
        print("failed:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
