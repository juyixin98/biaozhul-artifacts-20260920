#!/usr/bin/env python3
"""Deterministic end-to-end tests for the dagpaths JSON protocol.

Runs ./dagpaths as a subprocess for each case and checks the response
fields and the process exit code (0 on ok:true, 1 on ok:false).
"""
import json
import subprocess
import sys

BIN = "./dagpaths"

failures = []


def run_case(name, request, expect_ok, checks):
    """checks: dict of dotted-key -> expected value, plus optional
    'error.code' / callable checks."""
    raw = request if isinstance(request, str) else json.dumps(request)
    proc = subprocess.run([BIN], input=raw, capture_output=True, text=True)
    try:
        resp = json.loads(proc.stdout)
    except json.JSONDecodeError:
        failures.append(f"{name}: response is not valid JSON: {proc.stdout!r}")
        return
    if resp.get("ok") is not expect_ok:
        failures.append(f"{name}: ok={resp.get('ok')}, expected {expect_ok} ({resp})")
        return
    expected_code = 0 if expect_ok else 1
    if proc.returncode != expected_code:
        failures.append(f"{name}: exit code {proc.returncode}, expected {expected_code}")
    for key, expected in checks.items():
        node = resp
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node != expected:
            failures.append(f"{name}: {key}={node!r}, expected {expected!r}")


DIAMOND = {"num_nodes": 4, "edges": [[0, 1], [0, 2], [1, 3], [2, 3]]}
# Paths 0->4 in lex order: [0,1,3,4], [0,2,3,4], [0,4]
ORDER_G = {"num_nodes": 5, "edges": [[0, 1], [0, 2], [0, 4], [1, 3], [2, 3], [3, 4]]}
# Two sources (0,1), two reachable sinks (3,4), one isolated node (5).
MULTI = {"num_nodes": 6, "edges": [[0, 2], [1, 2], [2, 3], [2, 4]]}


def layered_graph(layers):
    """Layer 0: node 0. Layer k (1..layers): nodes 2k-1, 2k. Each node links
    to both nodes of the next layer. Paths from 0 to any layer-k node: 2^(k-1)
    (the first hop 0->1 or 0->2 is a single edge, then k-1 binary choices)."""
    edges = []
    for k in range(layers):
        for u in ([0] if k == 0 else [2 * k - 1, 2 * k]):
            edges.append([u, 2 * k + 1])
            edges.append([u, 2 * k + 2])
    return {"num_nodes": 2 * layers + 1, "edges": edges}


BIG = layered_graph(70)  # 2^69 paths to each layer-70 node
BIG_COUNT = str(2**69)   # 590295810358705651712, exceeds uint64 (2^64-1)

cases = [
    # --- count ---
    ("count_diamond", {"op": "count", "graph": DIAMOND, "source": 0, "target": 3},
     True, {"count": "2"}),
    ("count_unreachable", {"op": "count", "graph": DIAMOND, "source": 3, "target": 0},
     True, {"count": "0"}),
    ("count_multi_src_a", {"op": "count", "graph": MULTI, "source": 0, "target": 4},
     True, {"count": "1"}),
    ("count_multi_src_b", {"op": "count", "graph": MULTI, "source": 1, "target": 3},
     True, {"count": "1"}),
    ("count_isolated_sink", {"op": "count", "graph": MULTI, "source": 0, "target": 5},
     True, {"count": "0"}),
    ("count_source_eq_target", {"op": "count", "graph": DIAMOND, "source": 2, "target": 2},
     True, {"count": "1"}),
    ("count_bigint_2^70", {"op": "count", "graph": BIG, "source": 0, "target": 139},
     True, {"count": BIG_COUNT}),
    # --- kth ---
    ("kth_1", {"op": "kth", "graph": ORDER_G, "source": 0, "target": 4, "k": "1"},
     True, {"path": [0, 1, 3, 4]}),
    ("kth_2_int_k", {"op": "kth", "graph": ORDER_G, "source": 0, "target": 4, "k": 2},
     True, {"path": [0, 2, 3, 4]}),
    ("kth_3", {"op": "kth", "graph": ORDER_G, "source": 0, "target": 4, "k": "3"},
     True, {"path": [0, 4]}),
    ("kth_zero", {"op": "kth", "graph": ORDER_G, "source": 0, "target": 4, "k": "0"},
     False, {"error.code": "K_OUT_OF_RANGE"}),
    ("kth_beyond_count", {"op": "kth", "graph": ORDER_G, "source": 0, "target": 4, "k": "4"},
     False, {"error.code": "K_OUT_OF_RANGE"}),
    ("kth_unreachable", {"op": "kth", "graph": DIAMOND, "source": 3, "target": 0, "k": "1"},
     False, {"error.code": "K_OUT_OF_RANGE"}),
    ("kth_trivial_path", {"op": "kth", "graph": DIAMOND, "source": 2, "target": 2, "k": "1"},
     True, {"path": [2]}),
    ("kth_big_first", {"op": "kth", "graph": BIG, "source": 0, "target": 139, "k": "1"},
     True, {"path": [0] + [2 * k - 1 for k in range(1, 71)]}),
    ("kth_big_last", {"op": "kth", "graph": BIG, "source": 0, "target": 139, "k": BIG_COUNT},
     True, {"path": [0] + [2 * k for k in range(1, 70)] + [139]}),
    ("kth_big_beyond", {"op": "kth", "graph": BIG, "source": 0, "target": 139,
                        "k": str(2**69 + 1)},
     False, {"error.code": "K_OUT_OF_RANGE"}),
    # --- enumerate ---
    ("enumerate_all", {"op": "enumerate", "graph": ORDER_G, "source": 0, "target": 4},
     True, {"returned": 3, "truncated": False,
            "paths": [[0, 1, 3, 4], [0, 2, 3, 4], [0, 4]]}),
    ("enumerate_truncated",
     {"op": "enumerate", "graph": ORDER_G, "source": 0, "target": 4, "max_paths": 2},
     True, {"returned": 2, "truncated": True,
            "paths": [[0, 1, 3, 4], [0, 2, 3, 4]]}),
    ("enumerate_trivial", {"op": "enumerate", "graph": DIAMOND, "source": 2, "target": 2},
     True, {"returned": 1, "truncated": False, "paths": [[2]]}),
    ("enumerate_cap_zero",
     {"op": "enumerate", "graph": DIAMOND, "source": 0, "target": 3, "max_paths": 0},
     False, {"error.code": "LIMIT_EXCEEDED"}),
    # --- validation errors ---
    ("cycle", {"op": "count", "graph": {"num_nodes": 2, "edges": [[0, 1], [1, 0]]},
               "source": 0, "target": 1},
     False, {"error.code": "CYCLE"}),
    ("self_loop", {"op": "count", "graph": {"num_nodes": 2, "edges": [[0, 0]]},
                   "source": 0, "target": 1},
     False, {"error.code": "INVALID_GRAPH"}),
    ("duplicate_edge",
     {"op": "count", "graph": {"num_nodes": 2, "edges": [[0, 1], [0, 1]]},
      "source": 0, "target": 1},
     False, {"error.code": "DUPLICATE_EDGE"}),
    ("edge_node_out_of_range",
     {"op": "count", "graph": {"num_nodes": 2, "edges": [[0, 5]]}, "source": 0, "target": 1},
     False, {"error.code": "INVALID_GRAPH"}),
    ("endpoint_out_of_range",
     {"op": "count", "graph": DIAMOND, "source": 0, "target": 9},
     False, {"error.code": "INVALID_REQUEST"}),
    ("num_nodes_zero",
     {"op": "count", "graph": {"num_nodes": 0, "edges": []}, "source": 0, "target": 0},
     False, {"error.code": "LIMIT_EXCEEDED"}),
    ("missing_target", {"op": "count", "graph": DIAMOND, "source": 0},
     False, {"error.code": "MISSING_FIELD"}),
    ("missing_k", {"op": "kth", "graph": DIAMOND, "source": 0, "target": 3},
     False, {"error.code": "MISSING_FIELD"}),
    ("bad_k_type", {"op": "kth", "graph": DIAMOND, "source": 0, "target": 3, "k": "1x"},
     False, {"error.code": "TYPE_ERROR"}),
    ("unknown_op", {"op": "shortest", "graph": DIAMOND, "source": 0, "target": 3},
     False, {"error.code": "UNKNOWN_OP"}),
    ("malformed_json", "{not json", False, {"error.code": "PARSE_ERROR"}),
    ("request_not_object", "[1,2]", False, {"error.code": "INVALID_REQUEST"}),
]


def main():
    for name, request, expect_ok, checks in cases:
        run_case(name, request, expect_ok, checks)
    if failures:
        print(f"e2e_test: {len(failures)} failure(s):", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print(f"e2e_test: all {len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
