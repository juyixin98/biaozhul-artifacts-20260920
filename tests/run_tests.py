#!/usr/bin/env python3
"""Automated tests for the treewidth backend.

The tests do NOT trust the C++ embedded validation: every structural claim is
re-checked in Python from the serialized JSON (independent replay, edge
coverage, running intersection, tree shape).
"""
import json
import math
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "tdw_cli")
EX = os.path.join(ROOT, "examples")


def run_cli(mode, payload=None, extra_args=None):
    """Invoke the CLI; returns (returncode, parsed_stdout_or_None, stderr)."""
    args = [CLI, mode] + (extra_args or [])
    stdin = json.dumps(payload) if payload is not None else None
    p = subprocess.run(args, input=stdin, capture_output=True, text=True)
    out = None
    if p.stdout.strip():
        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError:
            out = None
    return p.returncode, out, p.stderr


def example(name):
    with open(os.path.join(EX, name)) as f:
        return json.load(f)


# ---------------- independent re-implementation of the checks -------------

def replay_width(n, edges, order):
    adj = [0] * n
    for u, v in edges:
        adj[u] |= 1 << v
        adj[v] |= 1 << u
    edge_set = {(min(u, v), max(u, v)) for u, v in edges}
    alive = (1 << n) - 1 if n else 0
    width = 0
    fills = set()
    for v in order:
        nb = adj[v] & alive
        ns = [i for i in range(n) if (nb >> i) & 1]
        width = max(width, len(ns))
        for i in range(len(ns)):
            for j in range(i + 1, len(ns)):
                a, b = ns[i], ns[j]
                key = (min(a, b), max(a, b))
                if key not in edge_set:
                    fills.add(key)
                adj[a] |= 1 << b
                adj[b] |= 1 << a
        alive &= ~(1 << v)
    return width, fills


def check_decomposition(n, edges, resp):
    """Returns list of problems (empty == valid). Independent of C++ code."""
    problems = []
    td = resp["tree_decomposition"]
    bags = [set(b["vertices"]) for b in td["bags"]]
    order = resp["elimination_order"]

    # order is a permutation
    if sorted(order) != list(range(n)):
        problems.append("order is not a permutation")

    # vertex coverage
    for v in range(n):
        if not any(v in b for b in bags):
            problems.append(f"vertex {v} uncovered")

    # edge + fill coverage
    alle = {(min(u, v), max(u, v)) for u, v in edges}
    for f in resp.get("fill_edges", []):
        alle.add((min(f), max(f)))
    for (u, v) in alle:
        if not any(u in b and v in b for b in bags):
            problems.append(f"edge {u},{v} in no bag")

    # bag graph is a tree
    bt = [tuple(e) for e in td["bag_tree_edges"]]
    if len(bags) > 0 and len(set(bt)) != n - 1:
        problems.append(f"bag edges {len(set(bt))} != n-1={n-1}")
    if len(bt) != len(set(bt)):
        problems.append("duplicate bag edges")
    neigh = [[] for _ in bags]
    for a, b in bt:
        neigh[a].append(b)
        neigh[b].append(a)
    if bags:
        seen = {0}
        stack = [0]
        while stack:
            x = stack.pop()
            for y in neigh[x]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        if seen != set(range(len(bags))):
            problems.append("bag graph not connected")

    # running intersection
    for v in range(n):
        hosts = {i for i, b in enumerate(bags) if v in b}
        if not hosts:
            continue
        reached = {next(iter(hosts))}
        stack = list(reached)
        while stack:
            x = stack.pop()
            for y in neigh[x]:
                if y in hosts and y not in reached:
                    reached.add(y)
                    stack.append(y)
        if reached != hosts:
            problems.append(f"running intersection violated for {v}")

    # width consistency
    if max((len(b) for b in bags), default=1) - 1 != resp["heuristic_width"]:
        problems.append("bag-derived width != reported heuristic_width")
    rw, _ = replay_width(n, edges, order)
    if rw != resp["heuristic_width"]:
        problems.append(f"independent replay width {rw} != reported")
    return problems


# --------------------------------- tests -----------------------------------

class TestKnownGraphs(unittest.TestCase):
    def assertValidDecomposition(self, resp, n, edges):
        self.assertEqual(resp["validation"]["valid"], True,
                         resp["validation"]["errors"])
        probs = check_decomposition(n, edges, resp)
        self.assertEqual(probs, [])

    def test_cycle6_width2(self):
        req = example("cycle6.json")
        rc, out, err = run_cli("solve", req)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["heuristic_width"], 2)  # tw(C_n)=2
        self.assertFalse(out["is_optimal"])
        self.assertIn("not_claimed_optimal", out["width_claim"])
        self.assertValidDecomposition(out, 6, req["edges"])

    def test_clique5_width4(self):
        req = example("clique5.json")
        rc, out, err = run_cli("solve", req)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["heuristic_width"], 4)  # tw(K_n)=n-1
        self.assertEqual(len(out["fill_edges"]), 0)  # already a clique
        self.assertValidDecomposition(out, 5, req["edges"])

    def test_two_disjoint_triangles_width2_multi_root(self):
        req = example("two_disjoint_triangles.json")
        rc, out, err = run_cli("solve", req)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["heuristic_width"], 2)  # max component tw
        self.assertValidDecomposition(out, 6, req["edges"])
        # exactly one cross-root link joining the two components
        self.assertEqual(len(out["tree_decomposition"]["bag_tree_edges"]), 5)

    def test_tree_width1(self):
        req = example("tree7.json")
        rc, out, err = run_cli("solve", req)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["heuristic_width"], 1)  # forests have tw <= 1
        self.assertValidDecomposition(out, 7, req["edges"])

    def test_edgeless_width0(self):
        req = example("edgeless3.json")
        rc, out, err = run_cli("solve", req)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["heuristic_width"], 0)
        self.assertValidDecomposition(out, 3, [])

    def test_chain_of_triangles(self):
        req = example("chain_of_three_triangles.json")
        rc, out, err = run_cli("solve", req)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["heuristic_width"], 2)
        self.assertValidDecomposition(out, 9, req["edges"])


class TestExactReference(unittest.TestCase):
    def test_exact_matches_known_treewidths(self):
        cases = [
            (example("cycle6.json"), 2),
            (example("clique5.json"), 4),
            (example("two_disjoint_triangles.json"), 2),
            (example("tree7.json"), 1),
            (example("edgeless3.json"), 0),
        ]
        for req, tw in cases:
            rc, out, err = run_cli("exact", req)
            self.assertEqual(rc, 0, err)
            self.assertEqual(out["optimal_width"], tw, req)
            self.assertEqual(out["permutations_examined"],
                             math.factorial(req["num_vertices"]))
            self.assertGreaterEqual(out["heuristic_width"], out["optimal_width"])

    def test_heuristic_optimal_on_all_small_graphs_n4(self):
        # Enumerate every labeled graph on <=4 vertices through the exact
        # endpoint; n=5 aggregate coverage is handled by the exhaustive test.
        for n in range(1, 5):
            pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
            for mask in range(1 << len(pairs)):
                edges = [p for k, p in enumerate(pairs) if (mask >> k) & 1]
                rc, out, err = run_cli(
                    "exact", {"num_vertices": n, "edges": edges})
                self.assertEqual(rc, 0, err)
                self.assertEqual(out["heuristic_width"], out["optimal_width"],
                                 (n, edges))

    def test_known_suboptimal_counterexample_n7(self):
        req = example("minfill_suboptimal_n7.json")
        rc, out, err = run_cli("exact", req)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["optimal_width"], 4)
        self.assertEqual(out["heuristic_width"], 5)
        self.assertEqual(out["heuristic_gap"], 1)
        self.assertFalse(out["heuristic_matches_optimum"])
        # even when suboptimal, the produced decomposition is still valid
        self.assertEqual(check_decomposition(7, req["edges"], out), [])


class TestExhaustiveMode(unittest.TestCase):
    def test_exhaustive_methods_agree(self):
        # Both exact methods must agree on the full labeled-graph census up
        # to n=5; agreement on the larger n=7 case is cross-checked per-graph
        # in TestExactReference.test_known_suboptimal_counterexample_n7.
        rc1, naive, e1 = run_cli("exhaustive", extra_args=["--n", "5"])
        rc2, fast, e2 = run_cli("exhaustive", extra_args=["--n", "5", "--fast"])
        self.assertEqual((rc1, rc2), (0, 0), e1 + e2)
        self.assertEqual(naive["total_graphs"], 1099)
        self.assertEqual(fast["total_graphs"], 1099)
        self.assertEqual(naive["total_graphs_with_suboptimal_heuristic"], 0)
        self.assertEqual(fast["total_graphs_with_suboptimal_heuristic"], 0)
        self.assertFalse(naive["external_solver_used"])
        self.assertFalse(fast["external_solver_used"])

    def test_exhaustive_n7_fast_finds_suboptimal_graphs(self):
        rc, out, err = run_cli("exhaustive", extra_args=["--n", "7", "--fast"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["total_graphs"], 2131019)
        self.assertEqual(out["total_graphs_with_suboptimal_heuristic"], 140)
        self.assertEqual(out["worst_gap_overall"], 1)
        self.assertGreaterEqual(len(out["suboptimal_examples"]), 1)


class TestVerifyMode(unittest.TestCase):
    def test_verify_accepts_good_response(self):
        rc, solve, err = run_cli("solve", example("cycle6.json"))
        self.assertEqual(rc, 0, err)
        p = subprocess.run([CLI, "verify"], input=json.dumps(solve),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        report = json.loads(p.stdout)["report"]
        self.assertTrue(report["valid"])
        self.assertEqual(report["running_intersection_violations"], 0)

    def test_verify_rejects_tampered_bag(self):
        rc, solve, err = run_cli("solve", example("cycle6.json"))
        self.assertEqual(rc, 0, err)
        # Bag 0 eliminates vertex 0 and equals {0, 1, 5}; removing vertex 1
        # leaves graph edge {0,1} in no bag.
        self.assertIn(1, solve["tree_decomposition"]["bags"][0]["vertices"])
        solve["tree_decomposition"]["bags"][0]["vertices"].remove(1)
        p = subprocess.run([CLI, "verify"], input=json.dumps(solve),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        report = json.loads(p.stdout)["report"]
        self.assertFalse(report["valid"])
        self.assertGreaterEqual(report["uncovered_edges"], 1)

    def test_verify_rejects_broken_running_intersection(self):
        req = example("clique5.json")
        rc, solve, err = run_cli("solve", req)
        self.assertEqual(rc, 0, err)
        # K5 bags form a chain 0-1-2-3-4 with bag i = {i,...,4}. Reroute the
        # tree to 0-2-1 while keeping it a connected 5-node tree: vertex 1
        # lives in bags {0,1}, whose unique path is now 0-2-1, and bag 2 does
        # not contain vertex 1 -> pure running-intersection violation.
        solve["tree_decomposition"]["bag_tree_edges"] = [
            [2, 0], [2, 1], [3, 2], [4, 3]
        ]
        p = subprocess.run([CLI, "verify"], input=json.dumps(solve),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        report = json.loads(p.stdout)["report"]
        self.assertFalse(report["valid"])
        self.assertGreaterEqual(report["running_intersection_violations"], 1)

    def test_verify_rejects_false_optimal_claim(self):
        rc, solve, err = run_cli("solve", example("cycle6.json"))
        self.assertEqual(rc, 0, err)
        solve["width_claim"] = "optimal"
        p = subprocess.run([CLI, "verify"], input=json.dumps(solve),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        report = json.loads(p.stdout)["report"]
        self.assertFalse(report["width_labeled_non_optimal"])


class TestInputValidation(unittest.TestCase):
    def test_bad_json(self):
        p = subprocess.run([CLI, "solve"], input="{not json",
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)

    def test_deeply_nested_json_rejected_gracefully(self):
        # Must return the malformed-input exit code, never crash (segfault).
        for mode in ("solve", "verify"):
            p = subprocess.run([CLI, mode], input="[" * 5000 + "0" + "]" * 5000,
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, mode)
            self.assertNotEqual(p.returncode, 139)
            self.assertIn("nesting depth", p.stderr)

    def test_vertex_out_of_range(self):
        rc, _, err = run_cli("solve", {"num_vertices": 3,
                                       "edges": [[0, 3]]})
        self.assertEqual(rc, 2)
        self.assertIn("out of range", err)

    def test_self_loop(self):
        rc, _, err = run_cli("solve", {"num_vertices": 3,
                                       "edges": [[1, 1]]})
        self.assertEqual(rc, 2)
        self.assertIn("self loop", err)

    def test_duplicate_edge(self):
        rc, _, err = run_cli("solve", {"num_vertices": 3,
                                       "edges": [[0, 1], [1, 0]]})
        self.assertEqual(rc, 2)
        self.assertIn("duplicate", err)

    def test_too_many_vertices(self):
        rc, _, err = run_cli("solve",
                             {"num_vertices": 65, "edges": []})
        self.assertEqual(rc, 2)

    def test_exact_refuses_large_n(self):
        rc, _, err = run_cli("exact", {"num_vertices": 11, "edges": []})
        self.assertEqual(rc, 2)
        self.assertIn("limited", err)


class TestBenchmark(unittest.TestCase):
    def test_bench_all_rows_valid(self):
        rc, out, err = run_cli("bench")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(out["rows"]), 24)
        for row in out["rows"]:
            self.assertTrue(row["independent_validation_valid"], row["case"])
            self.assertLessEqual(row["n"], 64)


if __name__ == "__main__":
    if not os.path.exists(CLI):
        print("build the binary first: make", file=sys.stderr)
        sys.exit(2)
    unittest.main(verbosity=2)
