#!/usr/bin/env python3
"""End-to-end acceptance tests for the bmatch CLI.

Generates random small bipartite graphs (including isolated vertices and
duplicate edges), computes the maximum matching by brute-force enumeration
in Python (independent of the C++ code), and checks the CLI response:
  - matching is valid and its size equals the brute-force optimum
  - vertex cover covers every edge
  - |cover| == |matching|  (Kőnig certificate)
  - original vertex IDs are preserved in the output
"""
import itertools
import json
import random
import subprocess
import sys

BMATCH = sys.argv[1] if len(sys.argv) > 1 else "build/bmatch"
TRIALS = int(sys.argv[2]) if len(sys.argv) > 2 else 300
SEED = int(sys.argv[3]) if len(sys.argv) > 3 else 20260925

failures = 0
checks = 0


def check(cond, what):
    global failures, checks
    checks += 1
    if not cond:
        failures += 1
        print(f"FAIL: {what}", file=sys.stderr)


def brute_force_max_matching(n_left, n_right, edges):
    """Exhaustive: try every subset of edges, keep the largest matching."""
    edge_set = sorted(set(edges))
    best = 0
    # Subset enumeration is fine: tests keep len(edge_set) <= 12.
    for r in range(len(edge_set) + 1):
        if r > len(edge_set):
            break
        for combo in itertools.combinations(edge_set, r):
            lefts = [e[0] for e in combo]
            rights = [e[1] for e in combo]
            if len(set(lefts)) == r and len(set(rights)) == r:
                best = max(best, r)
        if best == min(n_left, n_right, len(edge_set)):
            return best
    return best


def run_cli(request):
    proc = subprocess.run(
        [BMATCH], input=json.dumps(request), capture_output=True, text=True
    )
    return proc.returncode, proc.stdout.strip()


def verify_response(request, resp, ctx):
    n_left = len(request["left"])
    n_right = len(request["right"])
    edges = [(request["left"].index(l), request["right"].index(r))
             for l, r in request["edges"]]

    check(resp.get("ok") is True, f"{ctx}: ok is true")
    check(resp.get("verified") is True, f"{ctx}: verified is true")

    expected = brute_force_max_matching(n_left, n_right, edges)
    check(resp.get("matching_size") == expected,
          f"{ctx}: matching_size {resp.get('matching_size')} == brute-force {expected}")

    # Matching pairs reference original IDs and are consistent.
    left_ids = set(request["left"])
    right_ids = set(request["right"])
    edge_id_set = {(l, r) for l, r in request["edges"]}
    used_l, used_r = set(), set()
    for pair in resp.get("matching", []):
        l, r = pair["left"], pair["right"]
        check(l in left_ids and r in right_ids, f"{ctx}: pair IDs are original IDs")
        check((l, r) in edge_id_set, f"{ctx}: pair ({l},{r}) is an input edge")
        check(l not in used_l and r not in used_r, f"{ctx}: no vertex reused")
        used_l.add(l)
        used_r.add(r)
    check(len(resp.get("matching", [])) == resp.get("matching_size"),
          f"{ctx}: matching list length matches matching_size")

    # Vertex cover: covers every edge, size equals matching size.
    cover = resp.get("vertex_cover", {})
    cover_set = set(map(tuple, [("L", x) for x in cover.get("left", [])])) | \
                set(map(tuple, [("R", x) for x in cover.get("right", [])]))
    for l, r in request["edges"]:
        check(("L", l) in cover_set or ("R", r) in cover_set,
              f"{ctx}: edge ({l},{r}) covered")
    check(cover.get("size") == len(cover.get("left", [])) + len(cover.get("right", [])),
          f"{ctx}: cover size field consistent")
    check(cover.get("size") == resp.get("matching_size"),
          f"{ctx}: |cover| == |matching|")

    # Certificate self-check flags.
    cert = resp.get("certificate", {})
    for flag in ("matching_valid", "cover_covers_all_edges",
                 "cover_size_equals_matching_size"):
        check(cert.get(flag) is True, f"{ctx}: certificate.{flag}")


def random_request(rng):
    n_left = rng.randint(0, 6)
    n_right = rng.randint(0, 6)
    left = [f"L{i}" for i in range(n_left)]
    right = [f"R{j}" for j in range(n_right)]
    edges = []
    if left and right:
        for _ in range(rng.randint(0, 14)):
            # Random duplicates on purpose.
            edges.append([rng.choice(left), rng.choice(right)])
    return {"left": left, "right": right, "edges": edges}


def main():
    rng = random.Random(SEED)

    # Fixed edge cases: all-isolated graph, only duplicates, empty sides.
    fixed = [
        {"left": ["a", "b"], "right": ["x", "y"], "edges": []},
        {"left": ["a"], "right": ["x"], "edges": [["a", "x"], ["a", "x"], ["a", "x"]]},
        {"left": [], "right": [], "edges": []},
        {"left": ["a", "b", "c"], "right": ["x"], "edges": [["a", "x"], ["b", "x"], ["c", "x"]]},
    ]
    for i, req in enumerate(fixed):
        code, out = run_cli(req)
        check(code == 0, f"fixed#{i}: exit code 0 (got {code}: {out})")
        if code == 0:
            verify_response(req, json.loads(out), f"fixed#{i}")

    for t in range(TRIALS):
        req = random_request(rng)
        code, out = run_cli(req)
        check(code == 0, f"random#{t}: exit code 0 (got {code}: {out})")
        if code == 0:
            verify_response(req, json.loads(out), f"random#{t}")

    # Error handling: unknown vertex, duplicate vertex ID, malformed JSON.
    code, out = run_cli({"left": ["a"], "right": ["x"], "edges": [["b", "x"]]})
    check(code == 1 and json.loads(out).get("ok") is False,
          "unknown vertex rejected with ok:false")
    code, out = run_cli({"left": ["a", "a"], "right": ["x"], "edges": []})
    check(code == 1 and json.loads(out).get("ok") is False,
          "duplicate vertex ID rejected")
    proc = subprocess.run([BMATCH], input="{not json", capture_output=True, text=True)
    check(proc.returncode == 2 and json.loads(proc.stdout).get("ok") is False,
          "malformed JSON rejected")

    print(f"e2e checks: {checks}, failures: {failures} (seed {SEED}, trials {TRIALS})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
