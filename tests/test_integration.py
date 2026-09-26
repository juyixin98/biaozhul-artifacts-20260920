#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""End-to-end tests for the dagpaths backend.

Runs both transports (stdin line protocol and HTTP) against the same
binary, and cross-checks the C++ algorithms against an independent
Python reference implementation on many random small DAGs:

  * every (source, target) pair
  * enumeration order, total count
  * every valid k (kth <-> rank round-trip)
  * k past the end and k on unreachable pairs

Exits non-zero if anything fails, printing the actual commands/output.
"""

import argparse
import http.client
import itertools
import json
import os
import random
import socket
import subprocess
import sys
import time

PASS = 0
FAIL = 0
FAILURES = []


def report(condition, name, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append((name, detail))
        print(f"FAIL: {name}\n{detail}", file=sys.stderr)


def reference_enumerate(n, edges, s, t):
    """Independent DFS reference; adjacency in ascending node order."""
    adj = [[] for _ in range(n)]
    for u, v in edges:
        adj[u].append(v)
    for lst in adj:
        lst.sort()
    results = []
    stack = [(s, [s])]
    # Iterative DFS in sorted order so results are lexicographic.
    def dfs(u, prefix):
        if u == t:
            results.append(list(prefix))
            return
        for v in adj[u]:
            prefix.append(v)
            dfs(v, prefix)
            prefix.pop()
    dfs(s, [s])
    return results


def random_dag(rng, n, density):
    """Every edge i -> j (i < j) exists with the given probability."""
    edges = [[i, j] for i in range(n) for j in range(i + 1, n)
             if rng.random() < density]
    return n, edges


# --------------------------------------------------------------------------
# CLI transport
# --------------------------------------------------------------------------

class CliClient:
    def __init__(self, binary):
        self.binary = binary

    def call_many(self, requests, timeout=30):
        payload = "".join(json.dumps(r, separators=(",", ":")) + "\n"
                          for r in requests)
        proc = subprocess.run(
            [self.binary], input=payload, capture_output=True, text=True,
            timeout=timeout)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        if len(lines) != len(requests):
            raise AssertionError(
                f"expected {len(requests)} response lines, got {len(lines)}; "
                f"stderr={proc.stderr!r}")
        return [json.loads(ln) for ln in lines]

    def call(self, request, timeout=30):
        return self.call_many([request], timeout)[0]


# --------------------------------------------------------------------------
# HTTP transport
# --------------------------------------------------------------------------

class HttpClient:
    def __init__(self, binary, port):
        self.proc = subprocess.Popen(
            [binary, "--serve", "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.port = port
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), 0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise AssertionError("server failed to start: "
                             + self.proc.stderr.read())

    def call(self, request, timeout=30):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request("POST", "/", json.dumps(request),
                         {"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = resp.read().decode()
            report(resp.status == 200, "HTTP status 200",
                   f"status={resp.status} body={body}")
            return json.loads(body)
        finally:
            conn.close()

    def raw_post(self, body, method="POST", target="/"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, target, body,
                         {"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, resp.read().decode()
        finally:
            conn.close()

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def count_req(n, edges, s, t):
    return {"action": "count",
            "graph": {"nodes": n, "edges": edges},
            "source": s, "target": t}


def verify_graph_on_client(client, label, n, edges, exhaustive_k=True):
    """Full reference cross-check for every node pair in one graph."""
    pairs_checked = 0
    for s in range(n):
        for t in range(s, n):
            ref = reference_enumerate(n, edges, s, t)
            total = len(ref)
            resp = client.call(count_req(n, edges, s, t))
            report(resp.get("ok") is True,
                   f"[{label}] count ok {s}->{t}", json.dumps(resp))
            paths_field = resp.get("data", {}).get("paths")
            report(paths_field == str(total),
                   f"[{label}] count value {s}->{t}",
                   f"got {paths_field!r}, expected {total}")

            if total == 0:
                bad = client.call({"action": "kth",
                                   "graph": {"nodes": n, "edges": edges},
                                   "source": s, "target": t, "k": 1})
                report(bad.get("ok") is False
                       and bad["error"]["code"] == "K_OUT_OF_RANGE",
                       f"[{label}] unreachable kth {s}->{t}", json.dumps(bad))
                continue

            enum_resp = client.call({"action": "enumerate",
                                     "graph": {"nodes": n, "edges": edges},
                                     "source": s, "target": t})
            report(enum_resp["data"]["paths"] == ref,
                   f"[{label}] enumeration {s}->{t}",
                   f"got {enum_resp['data']['paths']}, expected {ref}")

            if not exhaustive_k or total > 600:
                ks = [1, total, (total + 1) // 2]
            else:
                ks = range(1, total + 1)
            for k in ks:
                kth = client.call({"action": "kth",
                                   "graph": {"nodes": n, "edges": edges},
                                   "source": s, "target": t,
                                   "k": str(k)})
                expected_path = ref[k - 1]
                report(kth.get("ok") is True and kth["data"]["path"] == expected_path,
                       f"[{label}] kth {s}->{t} k={k}",
                       f"got {json.dumps(kth)}, expected {expected_path}")

                rank = client.call({"action": "rank",
                                    "graph": {"nodes": n, "edges": edges},
                                    "path": expected_path})
                report(rank.get("ok") is True
                       and rank["data"]["rank"] == str(k)
                       and rank["data"]["total"] == str(total),
                       f"[{label}] rank {s}->{t} k={k}",
                       json.dumps(rank))
                pairs_checked += 1

            over = client.call({"action": "kth",
                                "graph": {"nodes": n, "edges": edges},
                                "source": s, "target": t,
                                "k": str(total + 1)})
            report(over.get("ok") is False
                   and over["error"]["code"] == "K_OUT_OF_RANGE",
                   f"[{label}] k=total+1 rejected {s}->{t}", json.dumps(over))
    return pairs_checked


def test_handcrafted(client, label):
    # Diamond: multi-path, lex order.
    n, edges = 4, [[0, 1], [0, 2], [1, 3], [2, 3]]
    verify_graph_on_client(client, f"{label}/diamond", n, edges)

    # Multiple sources and sinks + disconnected nodes.
    n, edges = 6, [[0, 1], [1, 2], [3, 4], [4, 5]]
    verify_graph_on_client(client, f"{label}/multi", n, edges)

    # Batch count: scalar/array source and target, including unreachable.
    resp = client.call({"action": "count",
                        "graph": {"nodes": n, "edges": edges},
                        "source": [0, 3, 2], "target": [2, 5, 0]})
    expected = {(0, 2): 1, (0, 5): 0, (0, 0): 1,
                (3, 2): 0, (3, 5): 1, (3, 0): 0,
                (2, 2): 1, (2, 5): 0, (2, 0): 0}
    got = {(r["source"], r["target"]): int(r["paths"])
           for r in resp["data"]["results"]}
    report(got == expected, f"[{label}] batch count matrix",
           f"got={got} expected={expected}")

    # s == t everywhere gives exactly 1.
    resp = client.call({"action": "count",
                        "graph": {"nodes": 4, "edges": [[0, 1], [2, 3]]},
                        "source": [0, 1, 2, 3], "target": [0, 1, 2, 3]})
    diag = [r["paths"] for r in resp["data"]["results"]
            if r["source"] == r["target"]]
    report(diag == ["1"] * 4, f"[{label}] empty path diagonal", str(diag))

    # Invalid graph: cycle.
    resp = client.call(count_req(3, [[0, 1], [1, 2], [2, 0]], 0, 2))
    report(resp["ok"] is False and resp["error"]["code"] == "INVALID_GRAPH",
           f"[{label}] cycle rejected", json.dumps(resp))

    # Invalid graph: self-loop / bad endpoint / duplicate.
    for bad_edges, name in [
        ([[0, 0]], "self-loop"),
        ([[0, 5]], "bad endpoint"),
        ([[0, 1], [0, 1]], "duplicate"),
    ]:
        resp = client.call(count_req(3, bad_edges, 0, 1))
        report(resp["ok"] is False
               and resp["error"]["code"] == "INVALID_GRAPH",
               f"[{label}] {name} rejected", json.dumps(resp))

    # Malformed requests.
    checks = [
        ({"action": "count", "graph": {"nodes": 1, "edges": []},
          "source": 0}, "missing target"),
        ({"action": "kth", "graph": {"nodes": 2, "edges": [[0, 1]]},
          "source": 0, "target": 1, "k": 0}, "k zero"),
        ({"action": "kth", "graph": {"nodes": 2, "edges": [[0, 1]]},
          "source": 0, "target": 1, "k": -1}, "k negative"),
        ({"action": "kth", "graph": {"nodes": 2, "edges": [[0, 1]]},
          "source": 0, "target": 1, "k": "abc"}, "k non-numeric"),
        ({"action": "rank", "graph": {"nodes": 3, "edges": [[0, 1]]},
          "path": [0, 2]}, "rank non-path"),
        ({"action": "rank", "graph": {"nodes": 3, "edges": [[0, 1]]},
          "path": []}, "rank empty path"),
        ({"action": "bogus", "graph": {"nodes": 1, "edges": []}},
         "unknown action"),
    ]
    for req, name in checks:
        resp = client.call(req)
        report(resp.get("ok") is False, f"[{label}] {name} rejected",
               json.dumps(resp))

    # Huge integer count: complete-forward 70 nodes => 2^68 paths.
    n = 70
    edges = [[i, j] for i in range(n) for j in range(i + 1, n)]
    total = 1 << (n - 2)
    resp = client.call(count_req(n, edges, 0, n - 1))
    report(resp["data"]["paths"] == str(total),
           f"[{label}] big count 2^68 exact", resp["data"]["paths"])
    kth = client.call({"action": "kth",
                       "graph": {"nodes": n, "edges": edges},
                       "source": 0, "target": n - 1, "k": str(total)})
    report(kth["data"]["path"] == [0, n - 1],
           f"[{label}] big k = total returns direct path",
           json.dumps(kth["data"]["path"]))
    rank = client.call({"action": "rank",
                        "graph": {"nodes": n, "edges": edges},
                        "path": [0, 1, n - 1]})
    # Paths starting [0,1,...]: 2^(67) of them (sub-paths from 1 to 69),
    # so [0,1,69] is the last of that block -> rank 2^67.
    report(rank["data"]["rank"] == str(1 << 67),
           f"[{label}] big rank exact", json.dumps(rank["data"]))
    over = client.call({"action": "kth",
                        "graph": {"nodes": n, "edges": edges},
                        "source": 0, "target": n - 1,
                        "k": str(total + 1)})
    report(over["error"]["code"] == "K_OUT_OF_RANGE",
           f"[{label}] big k total+1 rejected", json.dumps(over))


def test_random_graphs(client, label, seed=20260925):
    rng = random.Random(seed)
    total_pairs = 0
    configs = list(itertools.product(range(1, 9), (0.2, 0.5, 0.9)))
    for idx, (n, density) in enumerate(configs):
        n_nodes, edges = random_dag(rng, n, density)
        total_pairs += verify_graph_on_client(
            client, f"{label}/random#{idx} n={n} d={density}",
            n_nodes, edges)
    print(f"[{label}] random cross-check covered {total_pairs} rank/kth pairs")
    report(total_pairs > 0, f"[{label}] random cross-check ran any pairs")


def test_cli_framing(cli):
    # Multiple requests on consecutive lines get independent responses;
    # malformed JSON on one line must not kill the process for the next.
    good = count_req(3, [[0, 1], [1, 2]], 0, 2)
    responses = cli.call_many([good, good, good])
    report(all(r["ok"] for r in responses), "CLI three lines three responses")

    proc = subprocess.run(
        [cli.binary], input="{not json}\n" + json.dumps(good) + "\n",
        capture_output=True, text=True, timeout=10)
    lines = proc.stdout.splitlines()
    report(len(lines) == 2, "CLI malformed line still framed",
           f"stdout={proc.stdout!r}")
    report(json.loads(lines[0])["error"]["code"] == "INVALID_JSON",
           "CLI malformed line error code", lines[0])
    report(json.loads(lines[1])["ok"] is True,
           "CLI recovery after malformed line", lines[1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--skip-http", action="store_true")
    args = parser.parse_args()

    if not os.path.isfile(args.binary) or not os.access(args.binary, os.X_OK):
        print(f"binary not found or not executable: {args.binary}", file=sys.stderr)
        return 2

    cli = CliClient(args.binary)
    test_cli_framing(cli)
    test_handcrafted(cli, "cli")
    test_random_graphs(cli, "cli")

    if not args.skip_http:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        http_client = HttpClient(args.binary, port)
        try:
            test_handcrafted(http_client, "http")
            test_random_graphs(http_client, "http")
            status, body = http_client.raw_post("{}", method="GET")
            report(status == 400, "HTTP GET rejected with 400",
                   f"status={status} body={body}")
            status, body = http_client.raw_post("{not json")
            report(status == 200, "HTTP malformed JSON transported, app-level error",
                   f"status={status}")
            report(json.loads(body)["error"]["code"] == "INVALID_JSON",
                   "HTTP malformed JSON code", body)
        finally:
            http_client.stop()

    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL:
        print("Failures:")
        for name, detail in FAILURES[:20]:
            print(f"  - {name}: {detail[:200]}")
        return 1
    print("ALL INTEGRATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
