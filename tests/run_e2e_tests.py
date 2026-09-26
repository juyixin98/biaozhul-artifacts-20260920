#!/usr/bin/env python3
"""End-to-end and differential tests for the scc_analyze binary.

The Python side contains its OWN independent references:
  * SCC via an iterative Kosaraju
  * transitive closure via O(n^3) Floyd-Warshall
It never trusts the C++ output: partitions, DAG edges, multiplicities,
reachability matrices and cycle witnesses are all cross-checked.

No third-party packages are required (Python 3.8+).
"""

import json
import os
import random
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BINARY = os.path.join(ROOT, "scc_analyze")

FAILURES = []
CHECKS = 0


def check(cond, message):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(message)
        print(f"    FAIL: {message}")


def run_binary(payload):
    """Run the CLI on a JSON payload; return (returncode, parsed_stdout)."""
    proc = subprocess.run(
        [BINARY],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        parsed = json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return proc.returncode, parsed, proc.stderr


# ---------------------------------------------------------------------------
# Independent Python reference implementations
# ---------------------------------------------------------------------------

def warshall(n, edges):
    """Boolean reachability, edges = dict (u,v)->multiplicity."""
    reach = [[False] * n for _ in range(n)]
    for i in range(n):
        reach[i][i] = True
    for (u, v) in edges:
        reach[u][v] = True
    for k in range(n):
        rk = reach[k]
        for i in range(n):
            if reach[i][k]:
                ri = reach[i]
                for j in range(n):
                    if rk[j]:
                        ri[j] = True
    return reach


def kosaraju(n, edges):
    adj = [[] for _ in range(n)]
    radj = [[] for _ in range(n)]
    for (u, v) in edges:
        adj[u].append(v)
        radj[v].append(u)
    for lst in adj:
        lst.sort()
    for lst in radj:
        lst.sort()

    visited = [False] * n
    order = []
    for start in range(n):
        if visited[start]:
            continue
        visited[start] = True
        stack = [(start, 0)]
        while stack:
            u, idx = stack[-1]
            if idx < len(adj[u]):
                v = adj[u][idx]
                stack[-1] = (u, idx + 1)
                if not visited[v]:
                    visited[v] = True
                    stack.append((v, 0))
            else:
                order.append(u)
                stack.pop()

    comp = [-1] * n
    groups = []
    for start in reversed(order):
        if comp[start] != -1:
            continue
        cid = len(groups)
        groups.append([])
        comp[start] = cid
        stack = [start]
        while stack:
            u = stack.pop()
            groups[cid].append(u)
            for v in radj[u]:
                if comp[v] == -1:
                    comp[v] = cid
                    stack.append(v)

    # Canonical ids by ascending minimum vertex.
    mins = [min(g) for g in groups]
    remap = [0] * len(groups)
    for final_id, raw_id in enumerate(sorted(range(len(groups)), key=lambda c: mins[c])):
        remap[raw_id] = final_id
    return [remap[comp[v]] for v in range(n)]


def expected_dag(n, edges_counts, comp_of):
    dag = {}
    for (u, v), mult in edges_counts.items():
        cu, cv = comp_of[u], comp_of[v]
        if cu != cv:
            dag[(cu, cv)] = dag.get((cu, cv), 0) + mult
    return sorted((cu, cv, m) for (cu, cv), m in dag.items())


def validate_witness(n, adj_set, comp_of, comp_id, witness):
    """A witness is a closed simple walk: v0..vk plus implicit edge vk->v0."""
    if not witness:
        return False, "empty witness"
    if len(set(witness)) != len(witness):
        return False, "witness repeats a vertex"
    for v in witness:
        if comp_of[v] != comp_id:
            return False, "witness leaves component"
    for i in range(len(witness)):
        u = witness[i]
        v = witness[(i + 1) % len(witness)]
        if v not in adj_set[u]:
            return False, f"missing witness edge {u}->{v}"
    return True, ""


# ---------------------------------------------------------------------------
# Fixed cases required by the acceptance statement
# ---------------------------------------------------------------------------

def case_self_loop_multi_edge_isolated():
    print("[ RUN      ] fixed: self-loop + multi-edges + isolated vertices")
    payload = {
        "vertices": ["A", "B", "C", "D", "E"],
        "edges": [
            {"from": "A", "to": "B"},
            {"from": "B", "to": "A"},
            {"from": "A", "to": "B"},   # parallel edge
            {"from": "C", "to": "C"},   # self-loop
            {"from": "B", "to": "D"},
            {"from": "D", "to": "D"},   # self-loop inside bigger SCC attempt
            # E isolated
        ],
    }
    code, out, err = run_binary(payload)
    check(code == 0 and out and out.get("ok") is True, f"exit 0 and ok=true (stderr={err})")
    if not out:
        return

    n = 5
    raw = [(0, 1), (1, 0), (0, 1), (2, 2), (1, 3), (3, 3)]
    counts = {}
    for e in raw:
        counts[e] = counts.get(e, 0) + 1
    adj_set = [set() for _ in range(n)]
    for (u, v) in counts:
        adj_set[u].add(v)

    comp_of = kosaraju(n, counts)
    check(comp_of == [0, 0, 1, 2, 3], f"expected partition [0,0,1,2,3], got {comp_of}")

    got_comp_of = out["naiveReference"]["naiveComponentOf"]
    check(got_comp_of == comp_of, "C++ compOf matches Python Kosaraju")
    check(out["naiveReference"]["sccPartitionMatchesNaive"] is True,
          "C++ partition matches its internal naive reference")

    matrix = warshall(n, counts)
    check(out["naiveReference"]["reachabilityMatrix"] == [[1 if x else 0 for x in row] for row in matrix],
          "reachability matrix matches Floyd-Warshall reference")

    got_dag = [(e["from"], e["to"], e["multiplicity"]) for e in out["condensation"]["edges"]]
    check(got_dag == expected_dag(n, counts, comp_of), "DAG edges + multiplicities match reference")

    # Witnesses present exactly for cyclic components.
    for comp in out["components"]:
        cid = comp["id"]
        members = comp["vertices"]
        expect_cyclic = len(members) >= 2 or cid in [comp_of[2], comp_of[3]]
        check(comp["cyclic"] is expect_cyclic, f"cyclic flag for component {cid}")
        if comp["cyclic"]:
            ok, why = validate_witness(n, adj_set, comp_of, cid, comp["cycleWitness"])
            check(ok, f"valid witness for component {cid}: {why}")
        else:
            check(comp["cycleWitness"] == [], f"no witness for acyclic component {cid}")

    check(out["verification"]["ok"] is True, "independent verifier passed")


def case_empty_edges_all_isolated():
    print("[ RUN      ] fixed: all vertices isolated")
    payload = {"vertices": 4, "edges": []}
    code, out, err = run_binary(payload)
    check(code == 0 and out["ok"] is True, f"ok (stderr={err})")
    if out:
        check(out["stats"]["componentCount"] == 4, "4 singleton components")
        check(out["condensation"]["edges"] == [], "no condensation edges")
        check(all(not c["cyclic"] for c in out["components"]), "no cyclic components")
        check(out["verification"]["ok"] is True, "verifier passed")


def case_error_requests():
    print("[ RUN      ] fixed: malformed requests exit code 2")
    bad_payloads = [
        ("not json at all{", None),
        ({"vertices": 2, "edges": [{"from": 0, "to": 7}]}, "endpoint out of range"),
        ({"vertices": -1, "edges": []}, "negative vertex count"),
        ({"vertices": ["A", "A"], "edges": []}, "duplicate label"),
        ({"vertices": 3}, "missing edges"),
        ({"vertices": 3, "edges": [{"from": "X", "to": "A"}]}, "unknown label"),
    ]
    for payload, why in bad_payloads:
        if isinstance(payload, str):
            proc = subprocess.run([BINARY], input=payload, capture_output=True, text=True)
            code, out = proc.returncode, json.loads(proc.stdout)
        else:
            code, out, _ = run_binary(payload)
        check(code == 2 and out and out.get("ok") is False and "error" in out,
              f"rejected ({why}), got code={code} out={out}")


# ---------------------------------------------------------------------------
# Random differential testing
# ---------------------------------------------------------------------------

def random_graph(rng):
    n = rng.randint(1, 20)
    edge_count = rng.randint(0, n * 3)
    counts = {}
    for _ in range(edge_count):
        u = rng.randrange(n)
        v = rng.randrange(n)  # includes self-loops
        e = (u, v)
        counts[e] = counts.get(e, 0) + 1
    return n, counts


def differential_round(seed):
    rng = random.Random(seed)
    n, counts = random_graph(rng)

    # Emit with duplicated edges, possibly in shuffled order.
    raw = []
    for (u, v), mult in counts.items():
        raw.extend([{"from": u, "to": v}] * mult)
    rng.shuffle(raw)
    payload = {"vertices": n, "edges": raw}

    code, out, err = run_binary(payload)
    check(code == 0 and out and out.get("ok") is True,
          f"seed {seed}: binary failed: {err} {out}")
    if not out:
        return

    comp_of = kosaraju(n, counts)
    check(out["naiveReference"]["naiveComponentOf"] == comp_of,
          f"seed {seed}: partition mismatch")
    check(out["naiveReference"]["sccPartitionMatchesNaive"] is True,
          f"seed {seed}: internal naive mismatch")

    matrix = warshall(n, counts)
    expected_matrix = [[1 if x else 0 for x in row] for row in matrix]
    check(out["naiveReference"]["reachabilityMatrix"] == expected_matrix,
          f"seed {seed}: reachability matrix mismatch")

    got_dag = [(e["from"], e["to"], e["multiplicity"]) for e in out["condensation"]["edges"]]
    check(got_dag == expected_dag(n, counts, comp_of),
          f"seed {seed}: DAG mismatch, got {got_dag}")

    # Unique edges echoed back with exact multiplicities, deterministically sorted.
    got_unique = [(e["from"], e["to"], e["multiplicity"]) for e in out["uniqueEdges"]]
    check(got_unique == sorted((u, v, m) for (u, v), m in counts.items()),
          f"seed {seed}: unique-edge echo mismatch")

    adj_set = [set() for _ in range(n)]
    for (u, v) in counts:
        adj_set[u].add(v)
    for comp in out["components"]:
        if comp["cyclic"]:
            ok, why = validate_witness(n, adj_set, comp_of, comp["id"], comp["cycleWitness"])
            check(ok, f"seed {seed}: bad witness: {why}")

    check(out["verification"]["ok"] is True, f"seed {seed}: verifier failures {out['verification']}")

    # Determinism: same payload in reverse edge order must give same JSON body.
    payload_rev = {"vertices": n, "edges": list(reversed(raw))}
    code2, out2, _ = run_binary(payload_rev)
    for key in ("components", "condensation", "uniqueEdges"):
        check(out[key] == out2[key], f"seed {seed}: non-deterministic output in {key}")


def test_large_scale_limits():
    """One bigger graph to demonstrate the scale limits; naive reference off."""
    print("[ RUN      ] scale: 20000-vertex sparse DAG-ish graph")
    n = 20000
    edges = [{"from": i, "to": i + 1} for i in range(n - 1)]
    code, out, err = run_binary({"vertices": n, "edges": edges})
    check(code == 0 and out["ok"] is True, f"scale run ok (stderr={err})")
    if out:
        check(out["stats"]["componentCount"] == n, "20000 singleton SCCs in a chain")
        check(out["stats"]["dagEdgeCount"] == n - 1, "19999 DAG edges")
        check(out["naiveReference"]["enabled"] is False, "naive reference disabled above cutoff")
        check(out["verification"]["ok"] is True, "verifier passed at scale")
        print(f"             solver={out['stats']['solverMicros']}us "
              f"verify={out['stats']['verifyMicros']}us")


def main():
    if not os.path.exists(BINARY):
        print(f"binary not found at {BINARY}; run 'make' first", file=sys.stderr)
        return 2

    case_self_loop_multi_edge_isolated()
    case_empty_edges_all_isolated()
    case_error_requests()

    print("[ RUN      ] random differential tests (300 graphs)")
    for seed in range(300):
        differential_round(seed)

    test_large_scale_limits()

    print(f"\n{CHECKS} checks, {len(FAILURES)} failures")
    if FAILURES:
        for f in FAILURES[:20]:
            print(" -", f)
        return 1
    print("ALL E2E TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
